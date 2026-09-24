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
  MutationResult,
} from "@/model/protocol";
import type { ClientSettings, CustomView, ViewRule } from "@/model/settings";

export type FeedSelection = "all" | `domain:${string}` | `view:${string}`;

interface CompiledRule {
  domain?: RegExp;
  sender?: RegExp;
  tag?: RegExp;
}

interface CompiledView {
  id: string;
  name: string;
  rules: CompiledRule[];
}

export interface CustomViewSummary {
  id: string;
  name: string;
  notification_count: number;
  unread_count: number;
  pending_response_count: number;
  has_valid_rules: boolean;
}

function compileExpression(expression: string | undefined): RegExp | undefined {
  return expression === undefined ? undefined : new RegExp(expression, "i");
}

function compileRule(rule: ViewRule): CompiledRule | null {
  try {
    return {
      domain: compileExpression(rule.domain_regex),
      sender: compileExpression(rule.sender_regex),
      tag: compileExpression(rule.tag_regex),
    };
  } catch {
    return null;
  }
}

function compileView(view: CustomView): CompiledView {
  return {
    id: view.id,
    name: view.name,
    rules: view.rules.map(compileRule).filter((rule): rule is CompiledRule => rule !== null),
  };
}

function matchesRule(notification: Notification, rule: CompiledRule): boolean {
  return (!rule.domain || rule.domain.test(notification.domain))
    && (!rule.sender || rule.sender.test(notification.sender))
    && (!rule.tag || notification.tags.some((tag) => rule.tag!.test(tag)));
}

function matchesView(notification: Notification, view: CompiledView): boolean {
  return view.rules.some((rule) => matchesRule(notification, rule));
}

function notificationOrder(left: Notification, right: Notification): number {
  return right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id);
}

export function createHubStore() {
  const state = reactive({
    revision: 0,
    connection: { state: "starting", message: null } as ConnectionStatus,
    notifications: new Map<string, Notification>(),
    domains: new Map<string, DomainSummary>(),
    selection: "all" as FeedSelection,
    hideRead: false,
    compiledViews: [] as CompiledView[],
    pending: new Set<string>(),
    errors: new Map<string, { message: string; retryable: boolean }>(),
  });

  function replace(snapshot: Snapshot): void {
    state.notifications = new Map(snapshot.notifications.map((item) => [item.id, item]));
    state.domains = new Map(snapshot.domains.map((item) => [item.name, item]));
    validateSelection();
  }

  function upsert(notification: Notification): void {
    const current = state.notifications.get(notification.id);
    if (!current || notification.version > current.version) state.notifications.set(notification.id, notification);
  }

  function adopt(notification: Notification): void {
    const current = state.notifications.get(notification.id);
    if (!current || notification.version >= current.version) state.notifications.set(notification.id, notification);
  }

  function applyEvent(event: HubEvent): void {
    if (event.type === "notification.created" || event.type === "notification.updated") {
      upsert(event.notification);
    } else if (event.type === "notifications.read_state_changed") {
      for (const [index, id] of event.notification_ids.entries()) {
        const current = state.notifications.get(id);
        const version = event.versions[index];
        if (current && version !== undefined && version > current.version) {
          state.notifications.set(id, {
            ...current,
            read_at: event.read_at,
            version,
          });
        }
      }
    } else if (event.type === "notification.deleted") {
      state.notifications.delete(event.id);
    } else if (event.type === "domain.deleted") {
      state.domains.delete(event.name);
      if (state.selection === `domain:${event.name}`) state.selection = "all";
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

  function validateSelection(): void {
    if (state.selection.startsWith("domain:")) {
      if (!state.domains.has(state.selection.slice(7))) state.selection = "all";
      return;
    }
    if (state.selection.startsWith("view:")) {
      const selected = state.compiledViews.find((view) => view.id === state.selection.slice(5));
      if (!selected?.rules.length) state.selection = "all";
    }
  }

  function setSettings(settings: ClientSettings): void {
    state.hideRead = settings.hide_read;
    state.compiledViews = settings.views.map(compileView);
    validateSelection();
  }

  function select(selection: FeedSelection): void {
    state.selection = selection;
    validateSelection();
  }

  function applyBatch(batch: UpdateBatch): void {
    if (batch.revision < state.revision) return;
    batch.updates.forEach(applyUpdate);
    state.revision = batch.revision;
  }

  function beginMutation(ids: string[]): boolean {
    if (state.connection.state !== "connected" || ids.some((id) => state.pending.has(id))) return false;
    ids.forEach((id) => {
      state.pending.add(id);
      state.errors.delete(id);
    });
    return true;
  }

  function finishMutation(ids: string[], result: MutationResult): void {
    try {
      if (result.ok) {
        if (result.notification) adopt(result.notification);
        result.notifications?.forEach(adopt);
      } else {
        if (result.error.notification) adopt(result.error.notification);
        ids.forEach((id) => state.errors.set(id, {
          message: result.error.message,
          retryable: result.error.retryable,
        }));
      }
    } finally {
      ids.forEach((id) => state.pending.delete(id));
    }
  }

  function failMutation(ids: string[], error: unknown): void {
    const message = error instanceof Error ? error.message : "The operation failed.";
    ids.forEach((id) => {
      state.pending.delete(id);
      state.errors.set(id, { message, retryable: true });
    });
  }

  const domains = computed(() =>
    [...state.domains.values()].sort(
      (left, right) => right.last_activity_at.localeCompare(left.last_activity_at) || left.name.localeCompare(right.name),
    ),
  );
  const allNotifications = computed(() => [...state.notifications.values()]);
  const allSummary = computed(() => ({
    notification_count: allNotifications.value.length,
    unread_count: allNotifications.value.filter((item) => item.read_at === null).length,
    pending_response_count: allNotifications.value.filter((item) => item.response_state === "pending").length,
  }));
  const customViews = computed<CustomViewSummary[]>(() => state.compiledViews.map((view) => {
    const matching = allNotifications.value.filter((item) => matchesView(item, view));
    return {
      id: view.id,
      name: view.name,
      notification_count: matching.length,
      unread_count: matching.filter((item) => item.read_at === null).length,
      pending_response_count: matching.filter((item) => item.response_state === "pending").length,
      has_valid_rules: view.rules.length > 0,
    };
  }));
  const notifications = computed(() =>
    allNotifications.value
      .filter((item) => {
        if (state.selection === "all") return true;
        if (state.selection.startsWith("domain:")) return item.domain === state.selection.slice(7);
        const view = state.compiledViews.find((candidate) => candidate.id === state.selection.slice(5));
        return view ? matchesView(item, view) : false;
      })
      .filter((item) => !state.hideRead || item.read_at === null)
      .sort(notificationOrder),
  );

  return {
    state,
    domains,
    customViews,
    allSummary,
    notifications,
    hydrate,
    applyBatch,
    upsert,
    setSettings,
    select,
    beginMutation,
    finishMutation,
    failMutation,
  };
}

export type HubStore = ReturnType<typeof createHubStore>;
