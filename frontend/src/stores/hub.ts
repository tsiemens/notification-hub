import { computed, reactive } from "vue";

import type {
  BridgeUpdate,
  ConnectionStatus,
  DomainSummary,
  HubEvent,
  InitialState,
  Notification,
  Snapshot,
  UpdateBatch,
} from "@/model/protocol";

function notificationOrder(left: Notification, right: Notification): number {
  return right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id);
}

export function createHubStore() {
  const state = reactive({
    revision: 0,
    connection: { state: "starting", message: null } as ConnectionStatus,
    notifications: new Map<string, Notification>(),
    domains: new Map<string, DomainSummary>(),
    selectedDomain: null as string | null,
  });

  function replace(snapshot: Snapshot): void {
    state.notifications = new Map(snapshot.notifications.map((item) => [item.id, item]));
    state.domains = new Map(snapshot.domains.map((item) => [item.name, item]));
    if (state.selectedDomain && !state.domains.has(state.selectedDomain)) state.selectedDomain = null;
  }

  function upsert(notification: Notification): void {
    const current = state.notifications.get(notification.id);
    if (!current || notification.version > current.version) state.notifications.set(notification.id, notification);
  }

  function applyEvent(event: HubEvent): void {
    if (event.type === "notification.created" || event.type === "notification.updated") {
      upsert(event.notification);
    } else if (event.type === "notifications.read_state_changed") {
      for (const id of event.notification_ids) {
        const current = state.notifications.get(id);
        if (current) state.notifications.set(id, { ...current, read_at: event.read_at, version: current.version + 1 });
      }
    } else if (event.type === "notification.deleted") {
      state.notifications.delete(event.id);
    } else if (event.type === "domain.deleted") {
      state.domains.delete(event.name);
      if (state.selectedDomain === event.name) state.selectedDomain = null;
    }
  }

  function applyUpdate(update: BridgeUpdate): void {
    if (update.kind === "connection") state.connection = update.connection;
    else if (update.kind === "reset") replace(update.snapshot);
    else {
      update.events.forEach(applyEvent);
      state.domains = new Map(update.domains.map((item) => [item.name, item]));
    }
  }

  function hydrate(initial: InitialState): void {
    state.revision = initial.revision;
    state.connection = initial.connection;
    if (initial.snapshot) replace(initial.snapshot);
  }

  function applyBatch(batch: UpdateBatch): void {
    if (batch.revision < state.revision) return;
    batch.updates.forEach(applyUpdate);
    state.revision = batch.revision;
  }

  const domains = computed(() =>
    [...state.domains.values()].sort(
      (left, right) => right.last_activity_at.localeCompare(left.last_activity_at) || left.name.localeCompare(right.name),
    ),
  );
  const notifications = computed(() =>
    [...state.notifications.values()]
      .filter((item) => state.selectedDomain === null || item.domain === state.selectedDomain)
      .sort(notificationOrder),
  );

  return { state, domains, notifications, hydrate, applyBatch, upsert };
}

export type HubStore = ReturnType<typeof createHubStore>;
