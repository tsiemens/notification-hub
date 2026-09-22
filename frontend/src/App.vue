<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { desktopBridge } from "@/api/bridge";
import NotificationCard from "@/components/NotificationCard.vue";
import SettingsDialog from "@/components/SettingsDialog.vue";
import type { Notification, ResponseOption } from "@/model/protocol";
import type { ClientSettings } from "@/model/settings";
import { installTheme } from "@/presentation/theme";
import { createHubStore, type FeedSelection } from "@/stores/hub";

const store = createHubStore();
const domains = store.domains;
const customViews = store.customViews;
const allSummary = store.allSummary;
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
const unreadIds = computed(() => notifications.value.filter((item) => item.read_at === null).map((item) => item.id));
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
let removeThemeListener = (): void => undefined;

async function loadSettings(): Promise<void> {
  try {
    const loaded = await desktopBridge.getSettings();
    settings.value = loaded;
    store.setSettings(loaded);
  } catch (error) {
    settingsError.value = error instanceof Error ? error.message : "Settings could not be loaded.";
  }
}

function settingsSaved(saved: ClientSettings): void {
  settings.value = saved;
  store.setSettings(saved);
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

function resetFeedPosition(): void {
  windowSize.value = 50;
  showNew.value = false;
  nextTick(() => main.value?.scrollTo({ top: 0 }));
}

function select(selection: FeedSelection): void {
  store.select(selection);
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

async function markAllRead(): Promise<void> {
  const ids = unreadIds.value;
  if (!ids.length) return;
  if (ids.length >= 25 && !window.confirm(`Mark ${ids.length} notifications read?`)) return;
  if (!store.beginMutation(ids)) return;
  try {
    store.finishMutation(ids, await desktopBridge.setReadState(ids, true));
  } catch (error) {
    store.failMutation(ids, error);
  }
}

function unreadLabel(count: number): string {
  return `${count} unread notification${count === 1 ? "" : "s"}`;
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

watch([() => store.state.selection, () => store.state.hideRead], resetFeedPosition);
watch(() => settings.value?.theme, (theme) => {
  if (!theme) return;
  removeThemeListener();
  removeThemeListener = installTheme(theme);
});

onMounted(() => {
  void loadSettings();
  void synchronize();
  clock = window.setInterval(() => { now.value = Date.now(); }, 60_000);
});
onBeforeUnmount(() => {
  stopped = true;
  window.clearInterval(clock);
  removeThemeListener();
});
</script>

<template>
  <div class="app-shell">
    <header>
      <h1>Notification Hub</h1>
      <div class="header-actions">
        <div
          class="connection"
          role="status"
          tabindex="0"
          :aria-label="connectionMessage"
          :data-state="store.state.connection.state"
        >
          <svg class="connection-icon" viewBox="0 0 20 20" aria-hidden="true">
            <circle cx="10" cy="10" r="7" />
            <path v-if="store.state.connection.state === 'connected'" d="m6.5 10 2.2 2.2 4.8-5" />
            <path v-else-if="store.state.connection.state === 'starting'" d="M10 5v5l3 2" />
            <path v-else-if="store.state.connection.state === 'reconnecting'" d="M6 9a4 4 0 0 1 6.7-2.9L14 7.5M14 11a4 4 0 0 1-6.7 2.9L6 12.5" />
            <path v-else-if="store.state.connection.state === 'offline'" d="M7 7l6 6m0-6-6 6" />
            <path v-else d="M10 6.5v4.5m0 2.5v.1" />
          </svg>
          <span class="connection-popover" role="tooltip">{{ connectionMessage }}</span>
        </div>
        <button type="button" :disabled="!settings" @click="settingsOpen = true">Settings</button>
      </div>
    </header>
    <p v-if="fatal" class="fatal" role="alert">{{ fatal }}</p>
    <p v-if="settingsError" class="fatal" role="alert">{{ settingsError }}</p>
    <div class="workspace">
      <nav aria-label="Notification views and domains">
        <button :aria-current="store.state.selection === 'all'" @click="select('all')">
          <span class="nav-title"><strong>All Domains</strong><span v-if="allSummary.unread_count" class="unread-marker" role="img" :aria-label="unreadLabel(allSummary.unread_count)">● Unread</span></span>
          <span>{{ allSummary.notification_count }} notifications · {{ allSummary.unread_count }} unread · {{ allSummary.pending_response_count }} pending</span>
        </button>
        <button
          v-for="view in customViews"
          :key="`view:${view.id}`"
          :aria-current="store.state.selection === `view:${view.id}`"
          :disabled="!view.has_valid_rules"
          :title="view.has_valid_rules ? undefined : 'This view has no valid rules'"
          @click="select(`view:${view.id}`)"
        >
          <span class="nav-title">
            <span class="nav-label">
              <svg class="view-icon" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M2.5 3h11l-4.25 5v3.5l-2.5 1.5V8z" />
              </svg>
              <strong>{{ view.name }}</strong>
            </span>
            <span v-if="view.unread_count" class="unread-marker" role="img" :aria-label="unreadLabel(view.unread_count)">● Unread</span>
          </span>
          <span>{{ view.notification_count }} notifications · {{ view.unread_count }} unread · {{ view.pending_response_count }} pending</span>
        </button>
        <button v-for="domain in domains" :key="`domain:${domain.name}`" :aria-current="store.state.selection === `domain:${domain.name}`" @click="select(`domain:${domain.name}`)">
          <span class="nav-title"><strong>{{ domain.name }}</strong><span v-if="domain.unread_count" class="unread-marker" role="img" :aria-label="unreadLabel(domain.unread_count)">● Unread</span></span>
          <span>{{ domain.unread_count }} unread · {{ domain.pending_response_count }} pending</span>
          <small>{{ domain.latest_summary }}</small>
        </button>
      </nav>
      <main ref="main" aria-label="Notifications">
        <div class="feed-toolbar" aria-label="Current view actions">
          <span>{{ notifications.length }} notifications</span>
          <button type="button" :disabled="!connected || unreadIds.length === 0" @click="markAllRead">Mark all read</button>
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
          :raw-markdown="settings?.raw_markdown ?? false"
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
