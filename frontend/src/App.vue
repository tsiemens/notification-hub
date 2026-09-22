<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { desktopBridge } from "@/api/bridge";
import NotificationCard from "@/components/NotificationCard.vue";
import SettingsDialog from "@/components/SettingsDialog.vue";
import type { Notification, ResponseOption } from "@/model/protocol";
import type { ClientSettings } from "@/model/settings";
import { createHubStore } from "@/stores/hub";

const store = createHubStore();
const domains = store.domains;
const notifications = store.notifications;
const fatal = ref<string | null>(null);
const main = ref<HTMLElement | null>(null);
const windowSize = ref(50);
const showNew = ref(false);
const now = ref(Date.now());
const settings = ref<ClientSettings | null>(null);
const settingsOpen = ref(false);
const settingsError = ref<string | null>(null);
const renderedNotifications = computed(() => notifications.value.slice(0, windowSize.value));
const connected = computed(() => store.state.connection.state === "connected");
const connectionMessage = computed(() => {
  if (store.state.connection.message) return store.state.connection.message;
  return {
    starting: "Connecting to server…",
    connected: "Connected to server",
    reconnecting: "Reconnecting to server…",
    offline: "Server is offline",
    fatal: "Connection failed; check the client configuration",
  }[store.state.connection.state];
});
let stopped = false;
let clock = 0;

async function loadSettings(): Promise<void> {
  try {
    settings.value = await desktopBridge.getSettings();
  } catch (error) {
    settingsError.value = error instanceof Error ? error.message : "Settings could not be loaded.";
  }
}

function settingsSaved(saved: ClientSettings): void {
  settings.value = saved;
  settingsOpen.value = false;
}

async function synchronize(): Promise<void> {
  try {
    store.hydrate(await desktopBridge.getInitialState());
    while (!stopped) store.applyBatch(await desktopBridge.getUpdates(store.state.revision));
  } catch (error) {
    fatal.value = error instanceof Error ? error.message : "The desktop bridge failed.";
  }
}

function selectDomain(domain: string | null): void {
  store.state.selectedDomain = domain;
  windowSize.value = 50;
  showNew.value = false;
  nextTick(() => main.value?.scrollTo({ top: 0 }));
}

async function showOlder(): Promise<void> {
  windowSize.value += 50;
  await nextTick();
}

function returnToNewest(): void {
  showNew.value = false;
  main.value?.scrollTo({ top: 0, behavior: "smooth" });
}

async function setRead(notification: Notification, read: boolean): Promise<void> {
  const ids = [notification.id];
  if (!store.beginMutation(ids)) return;
  try {
    store.finishMutation(ids, await desktopBridge.setReadState(ids, read));
  } catch (error) {
    store.failMutation(ids, error);
  }
}

async function bulkRead(read: boolean): Promise<void> {
  const ids = notifications.value.map((item) => item.id);
  if (!ids.length) return;
  if (ids.length >= 25 && !window.confirm(`Mark ${ids.length} notifications ${read ? "read" : "unread"}?`)) return;
  if (!store.beginMutation(ids)) return;
  try {
    store.finishMutation(ids, await desktopBridge.setReadState(ids, read));
  } catch (error) {
    store.failMutation(ids, error);
  }
}

async function respond(notification: Notification, option: ResponseOption, message: string | null): Promise<void> {
  const ids = [notification.id];
  if (!store.beginMutation(ids)) return;
  try {
    store.finishMutation(ids, await desktopBridge.respond(notification.id, option.id, message));
  } catch (error) {
    store.failMutation(ids, error);
  }
}

function renderedCard(id: string): HTMLElement | undefined {
  return [...(main.value?.querySelectorAll<HTMLElement>("article[data-notification-id]") ?? [])]
    .find((element) => element.dataset.notificationId === id);
}

watch(() => notifications.value[0]?.id, (head, oldHead) => {
  if (!oldHead || !head || head === oldHead) return;
  const element = main.value;
  // If the new head was already rendered, the old head was removed or the
  // view changed; this is not a newly prepended notification.
  if (renderedCard(head)) return;
  if ((element?.scrollTop ?? 0) > 120) {
    const anchor = renderedCard(oldHead);
    const oldTop = anchor?.getBoundingClientRect().top;
    showNew.value = true;
    nextTick(() => {
      if (!element || oldTop === undefined) return;
      const newTop = renderedCard(oldHead)?.getBoundingClientRect().top;
      if (newTop !== undefined) element.scrollTop += newTop - oldTop;
    });
  } else {
    nextTick(() => element?.scrollTo({ top: 0 }));
  }
});

onMounted(() => {
  void loadSettings();
  void synchronize();
  clock = window.setInterval(() => { now.value = Date.now(); }, 60_000);
});
onBeforeUnmount(() => {
  stopped = true;
  window.clearInterval(clock);
});
</script>

<template>
  <div class="app-shell">
    <header>
      <h1>Notification Hub</h1>
      <div class="header-actions">
        <p class="connection" role="status" :data-state="store.state.connection.state">{{ connectionMessage }}</p>
        <button type="button" :disabled="!settings" @click="settingsOpen = true">Settings</button>
      </div>
    </header>
    <p v-if="fatal" class="fatal" role="alert">{{ fatal }}</p>
    <p v-if="settingsError" class="fatal" role="alert">{{ settingsError }}</p>
    <div class="workspace">
      <nav aria-label="Notification domains">
        <button :aria-current="store.state.selectedDomain === null" @click="selectDomain(null)">
          <strong>All Domains</strong>
          <span>{{ store.state.notifications.size }} notifications</span>
        </button>
        <button v-for="domain in domains" :key="domain.name" :aria-current="store.state.selectedDomain === domain.name" @click="selectDomain(domain.name)">
          <strong>{{ domain.name }}</strong>
          <span>{{ domain.unread_count }} unread · {{ domain.pending_response_count }} pending</span>
          <small>{{ domain.latest_summary }}</small>
        </button>
      </nav>
      <main ref="main" aria-label="Notifications">
        <div class="feed-toolbar" aria-label="Current view actions">
          <span>{{ notifications.length }} notifications</span>
          <div>
            <button type="button" :disabled="!connected || notifications.length === 0" @click="bulkRead(true)">Mark view read</button>
            <button type="button" :disabled="!connected || notifications.length === 0" @click="bulkRead(false)">Mark view unread</button>
          </div>
        </div>
        <button v-if="showNew" class="new-notifications" type="button" @click="returnToNewest">New notifications · return to top</button>
        <p v-if="notifications.length === 0" class="empty">No notifications</p>
        <NotificationCard
          v-for="notification in renderedNotifications"
          :key="notification.id"
          :notification="notification"
          :bridge="desktopBridge"
          :pending="store.state.pending.has(notification.id)"
          :error="store.state.errors.get(notification.id)"
          :connected="connected"
          :now="now"
          @read="setRead"
          @respond="respond"
        />
        <button v-if="renderedNotifications.length < notifications.length" type="button" class="show-older" @click="showOlder">
          Show older notifications ({{ notifications.length - renderedNotifications.length }} remaining)
        </button>
      </main>
    </div>
    <SettingsDialog v-if="settingsOpen && settings" :settings="settings" :bridge="desktopBridge" @close="settingsOpen = false" @saved="settingsSaved" />
  </div>
</template>
