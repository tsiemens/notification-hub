import { describe, expect, it } from "vitest";

import type { InitialState, Notification } from "@/model/protocol";
import { createHubStore } from "@/stores/hub";

function notification(id: string, created_at: string, version = 1, overrides: Partial<Notification> = {}): Notification {
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
    ...overrides,
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
    store.select("domain:tests");
    store.applyBatch({
      revision: 2,
      updates: [{ kind: "events", domains: [], events: [
        { seq: 4, type: "notification.deleted", id: "a" },
        { seq: 5, type: "domain.deleted", name: "tests" },
      ] }],
    });

    expect(store.notifications.value).toEqual([]);
    expect(store.state.selection).toBe("all");
  });

  it("uses OR between rules, AND within rules, and any-tag matching", () => {
    const store = createHubStore();
    store.hydrate(initial([
      notification("and", "2026-01-04T00:00:00.000Z", 1, { domain: "Ops", sender: "Deploy Bot", tags: ["release", "blue"] }),
      notification("wrong-sender", "2026-01-03T00:00:00.000Z", 1, { domain: "ops", sender: "human", tags: ["release"] }),
      notification("tag-rule", "2026-01-02T00:00:00.000Z", 1, { domain: "other", sender: "human", tags: ["routine", "URGENT-fix"] }),
      notification("no-match", "2026-01-01T00:00:00.000Z", 1, { domain: "other", sender: "human", tags: ["routine"] }),
    ]));
    store.setSettings({
      theme: "system", sound: "never", sound_path: "", hide_read: false, raw_markdown: false,
      views: [{ id: "ops", name: "Ops", rules: [
        { domain_regex: "^ops$", sender_regex: "deploy" },
        { tag_regex: "urgent" },
      ] }],
    });
    store.select("view:ops");

    expect(store.notifications.value.map((item) => item.id)).toEqual(["and", "tag-rule"]);
  });

  it("omits only invalid regex rules and rejects a view with no valid rules", () => {
    const store = createHubStore();
    store.hydrate(initial([notification("match", "2026-01-01T00:00:00.000Z", 1, { sender: "robot" })]));
    store.setSettings({
      theme: "system", sound: "never", sound_path: "", hide_read: false, raw_markdown: false,
      views: [
        { id: "mixed", name: "Mixed", rules: [{ domain_regex: "[" }, { sender_regex: "ROBOT" }] },
        { id: "broken", name: "Broken", rules: [{ tag_regex: "(" }] },
      ],
    });
    store.select("view:mixed");
    expect(store.notifications.value.map((item) => item.id)).toEqual(["match"]);
    expect(store.customViews.value.map((view) => view.has_valid_rules)).toEqual([true, false]);

    store.select("view:broken");
    expect(store.state.selection).toBe("all");
  });

  it("keeps domain and view selections distinct when their names collide", () => {
    const store = createHubStore();
    const domainItem = notification("domain", "2026-01-02T00:00:00.000Z", 1, { domain: "shared", sender: "human" });
    const viewItem = notification("view", "2026-01-01T00:00:00.000Z", 1, { domain: "elsewhere", sender: "robot" });
    store.hydrate({ ...initial([domainItem, viewItem]), snapshot: { sequence: 1, notifications: [domainItem, viewItem], domains: [{
      name: "shared", last_activity_at: domainItem.created_at, notification_count: 1, unread_count: 1,
      pending_response_count: 0, latest_summary: domainItem.summary,
    }] } });
    store.setSettings({
      theme: "system", sound: "never", sound_path: "", hide_read: false, raw_markdown: false,
      views: [{ id: "shared", name: "shared", rules: [{ sender_regex: "robot" }] }],
    });

    store.select("domain:shared");
    expect(store.notifications.value.map((item) => item.id)).toEqual(["domain"]);
    store.select("view:shared");
    expect(store.notifications.value.map((item) => item.id)).toEqual(["view"]);
  });

  it("updates live view membership and derives counts from the complete local store", () => {
    const store = createHubStore();
    const item = notification("a", "2026-01-01T00:00:00.000Z", 1, { domain: "ops", sender: "human" });
    store.hydrate(initial([item]));
    store.setSettings({
      theme: "system", sound: "never", sound_path: "", hide_read: false, raw_markdown: false,
      views: [{ id: "bots", name: "Bots", rules: [{ sender_regex: "bot" }] }],
    });
    store.select("view:bots");
    expect(store.notifications.value).toEqual([]);

    store.applyBatch({ revision: 2, updates: [{ kind: "events", domains: [], events: [{
      seq: 2, type: "notification.updated", notification: { ...item, sender: "Bot", response_state: "pending", version: 2 },
    }] }] });
    expect(store.notifications.value.map((entry) => entry.id)).toEqual(["a"]);
    expect(store.customViews.value[0]).toMatchObject({ notification_count: 1, unread_count: 1, pending_response_count: 1 });
  });

  it("applies hide-read last and restores locally retained read items", () => {
    const store = createHubStore();
    const unread = notification("unread", "2026-01-02T00:00:00.000Z", 1, { domain: "ops" });
    const read = notification("read", "2026-01-01T00:00:00.000Z", 1, { domain: "ops", read_at: "2026-01-03T00:00:00.000Z" });
    store.hydrate(initial([unread, read]));
    const settings = {
      theme: "system" as const, sound: "never" as const, sound_path: "", raw_markdown: false,
      views: [{ id: "ops", name: "Ops", rules: [{ domain_regex: "ops" }] }],
    };
    store.setSettings({ ...settings, hide_read: true });
    store.select("view:ops");
    expect(store.notifications.value.map((item) => item.id)).toEqual(["unread"]);
    expect(store.state.notifications.has("read")).toBe(true);

    store.setSettings({ ...settings, hide_read: false });
    expect(store.notifications.value.map((item) => item.id)).toEqual(["unread", "read"]);
  });

  it("returns to all when the active view is removed", () => {
    const store = createHubStore();
    store.setSettings({
      theme: "system", sound: "never", sound_path: "", hide_read: false, raw_markdown: false,
      views: [{ id: "temporary", name: "Temporary", rules: [{ domain_regex: "." }] }],
    });
    store.select("view:temporary");
    store.setSettings({ theme: "system", sound: "never", sound_path: "", hide_read: false, raw_markdown: false, views: [] });
    expect(store.state.selection).toBe("all");
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
