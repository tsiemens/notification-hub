<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { desktopBridge } from "@/api/bridge";
import { createHubStore } from "@/stores/hub";

const store = createHubStore();
const domains = store.domains;
const notifications = store.notifications;
const fatal = ref<string | null>(null);
const connectionMessage = computed(() => {
  if (store.state.connection.message) return store.state.connection.message;
  return {
    starting: "Connecting to server…",
    connected: "Connected to server",
    reconnecting: "Reconnecting to server…",
    offline: "Server is offline",
    fatal: "Connection failed",
  }[store.state.connection.state];
});
let stopped = false;

async function synchronize(): Promise<void> {
  try {
    store.hydrate(await desktopBridge.getInitialState());
    while (!stopped) {
      store.applyBatch(await desktopBridge.getUpdates(store.state.revision));
    }
  } catch (error) {
    fatal.value = error instanceof Error ? error.message : "The desktop bridge failed.";
  }
}

onMounted(synchronize);
onBeforeUnmount(() => {
  stopped = true;
});
</script>

<template>
  <div class="app-shell">
    <header>
      <h1>Notification Hub</h1>
      <p class="connection" role="status" :data-state="store.state.connection.state">
        {{ connectionMessage }}
      </p>
    </header>
    <p v-if="fatal" class="fatal" role="alert">{{ fatal }}</p>
    <div class="workspace">
      <nav aria-label="Notification domains">
        <button :aria-current="store.state.selectedDomain === null" @click="store.state.selectedDomain = null">
          All Domains
        </button>
        <button
          v-for="domain in domains"
          :key="domain.name"
          :aria-current="store.state.selectedDomain === domain.name"
          @click="store.state.selectedDomain = domain.name"
        >
          <strong>{{ domain.name }}</strong>
          <span>{{ domain.unread_count }} unread · {{ domain.pending_response_count }} pending</span>
        </button>
      </nav>
      <main aria-label="Notifications">
        <p v-if="notifications.length === 0" class="empty">No notifications</p>
        <article v-for="notification in notifications.slice(0, 100)" :key="notification.id">
          <div class="metadata">{{ notification.domain }} · {{ notification.sender }}</div>
          <h2>{{ notification.summary }}</h2>
          <p class="message">{{ notification.message }}</p>
          <div v-if="notification.tags.length" class="tags">
            <span v-for="tag in notification.tags" :key="tag">{{ tag }}</span>
          </div>
        </article>
      </main>
    </div>
  </div>
</template>
