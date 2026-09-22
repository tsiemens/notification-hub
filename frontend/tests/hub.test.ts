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
});
