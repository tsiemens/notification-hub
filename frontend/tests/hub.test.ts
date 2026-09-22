import { describe, expect, it } from "vitest";

import type { InitialState, Notification } from "@/model/protocol";
import { createHubStore } from "@/stores/hub";

function notification(id: string, created_at: string, version = 1): Notification {
  return {
    id,
    domain: "tests",
    sender: "vitest",
    summary: id,
    message: "",
    details: null,
    tags: [],
    priority: "normal",
    source_created_at: null,
    created_at,
    updated_at: created_at,
    read_at: null,
    response_state: "not_requested",
    response_options: [],
    response: null,
    version,
  };
}

function initial(items: Notification[]): InitialState {
  return {
    revision: 1,
    connection: { state: "connected", message: null },
    snapshot: { sequence: 3, domains: [], notifications: items },
  };
}

describe("hub store", () => {
  it("orders every feed newest first with id as the tie breaker", () => {
    const store = createHubStore();
    store.hydrate(initial([
      notification("a", "2026-01-01T00:00:00.000Z"),
      notification("b", "2026-01-02T00:00:00.000Z"),
      notification("c", "2026-01-02T00:00:00.000Z"),
    ]));
    expect(store.notifications.value.map((item) => item.id)).toEqual(["c", "b", "a"]);
  });

  it("does not replace a record with a stale event", () => {
    const store = createHubStore();
    const current = notification("a", "2026-01-01T00:00:00.000Z", 3);
    store.hydrate(initial([current]));
    store.applyBatch({
      revision: 2,
      updates: [{
        kind: "events",
        domains: [],
        events: [{ seq: 4, type: "notification.updated", notification: { ...current, summary: "stale", version: 2 } }],
      }],
    });
    expect(store.notifications.value[0].summary).toBe("a");
  });

  it("handles notification/domain deletion and resets invalid selection", () => {
    const store = createHubStore();
    const item = notification("a", "2026-01-01T00:00:00.000Z");
    store.hydrate({
      ...initial([item]),
      snapshot: {
        sequence: 3,
        notifications: [item],
        domains: [{
          name: "tests",
          last_activity_at: item.created_at,
          notification_count: 1,
          unread_count: 1,
          pending_response_count: 0,
          latest_summary: item.summary,
        }],
      },
    });
    store.state.selectedDomain = "tests";
    store.applyBatch({
      revision: 2,
      updates: [{ kind: "events", domains: [], events: [
        { seq: 4, type: "notification.deleted", id: "a" },
        { seq: 5, type: "domain.deleted", name: "tests" },
      ] }],
    });

    expect(store.notifications.value).toEqual([]);
    expect(store.state.selectedDomain).toBeNull();
  });

  it("keeps the newer event when a mutation result loses a race", () => {
    const store = createHubStore();
    const original = notification("a", "2026-01-01T00:00:00.000Z", 1);
    store.hydrate(initial([original]));
    expect(store.beginMutation(["a"])).toBe(true);
    store.applyBatch({
      revision: 2,
      updates: [{ kind: "events", domains: [], events: [{
        seq: 4,
        type: "notification.updated",
        notification: { ...original, summary: "event wins", version: 3 },
      }] }],
    });
    store.finishMutation(["a"], { ok: true, notification: { ...original, summary: "stale result", version: 2 } });

    expect(store.notifications.value[0].summary).toBe("event wins");
    expect(store.state.pending.has("a")).toBe(false);
  });

  it("advances versions for new read-state events", () => {
    const store = createHubStore();
    const original = notification("a", "2026-01-01T00:00:00.000Z", 2);
    store.hydrate(initial([original]));
    store.applyBatch({
      revision: 2,
      updates: [{ kind: "events", domains: [], events: [{
        seq: 4,
        type: "notifications.read_state_changed",
        notification_ids: ["a"],
        read_at: "2026-01-02T00:00:00.000Z",
      }] }],
    });
    store.applyBatch({
      revision: 3,
      updates: [{ kind: "events", domains: [], events: [{
        seq: 5,
        type: "notification.updated",
        notification: { ...original, summary: "response accepted", version: 4 },
      }] }],
    });

    expect(store.notifications.value[0].summary).toBe("response accepted");
    expect(store.notifications.value[0].version).toBe(4);
  });

  it("does not count a read-state event twice when its mutation result arrived first", () => {
    const store = createHubStore();
    const original = notification("a", "2026-01-01T00:00:00.000Z", 1);
    store.hydrate(initial([original]));
    store.finishMutation(["a"], {
      ok: true,
      notifications: [{ ...original, read_at: "2026-01-02T00:00:00.000Z", version: 2 }],
    });
    store.applyBatch({
      revision: 2,
      updates: [{ kind: "events", domains: [], events: [{
        seq: 4,
        type: "notifications.read_state_changed",
        notification_ids: ["a"],
        read_at: "2026-01-02T00:00:00.000Z",
      }] }],
    });

    expect(store.notifications.value[0].version).toBe(2);
  });

  it("rejects a stale mutation result after a later read-state event", () => {
    const store = createHubStore();
    const original = notification("a", "2026-01-01T00:00:00.000Z", 1);
    store.hydrate(initial([original]));
    store.applyBatch({
      revision: 2,
      updates: [{ kind: "events", domains: [], events: [
        { seq: 4, type: "notifications.read_state_changed", notification_ids: ["a"], read_at: "2026-01-02T00:00:00.000Z" },
        { seq: 5, type: "notifications.read_state_changed", notification_ids: ["a"], read_at: null },
      ] }],
    });
    store.finishMutation(["a"], {
      ok: true,
      notifications: [{ ...original, read_at: "2026-01-02T00:00:00.000Z", version: 2 }],
    });

    expect(store.notifications.value[0].read_at).toBeNull();
    expect(store.notifications.value[0].version).toBe(3);
  });

  it("adopts an already-answered winner and exposes a retryable error", () => {
    const store = createHubStore();
    const original = notification("a", "2026-01-01T00:00:00.000Z", 1);
    store.hydrate(initial([original]));
    store.beginMutation(["a"]);
    store.finishMutation(["a"], {
      ok: false,
      error: {
        code: "already_answered",
        message: "Another client answered first.",
        retryable: false,
        notification: { ...original, summary: "winner", version: 2 },
      },
    });

    expect(store.notifications.value[0].summary).toBe("winner");
    expect(store.state.errors.get("a")?.message).toContain("answered first");
  });
});
