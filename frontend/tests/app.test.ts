import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const items = Array.from({ length: 120 }, (_, index) => ({
    id: String(index), domain: "large", sender: "tests", summary: `Notice ${index}`, message: "", details: null,
    tags: [], priority: "normal" as const, source_created_at: null,
    created_at: new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString(), read_at: null,
    response_state: "not_requested" as const, response_options: [], response: null, version: 1,
  }));
  const setReadState = vi.fn(async (ids: string[]) => ({ ok: true as const, notifications: ids.map((id) => ({ ...items[Number(id)], read_at: "2026-01-02T00:00:00.000Z", version: 2 })) }));
  const updateResolvers: Array<(value: unknown) => void> = [];
  return { items, setReadState, updateResolvers };
});

vi.mock("@/api/bridge", () => ({
  desktopBridge: {
    getInitialState: vi.fn(async () => ({ revision: 1, connection: { state: "connected", message: null }, snapshot: {
      sequence: 1,
      domains: [{ name: "large", last_activity_at: mocks.items[119].created_at, notification_count: 120, unread_count: 120, pending_response_count: 0, latest_summary: "Notice 119" }],
      notifications: mocks.items,
    } })),
    getUpdates: vi.fn(() => new Promise((resolve) => mocks.updateResolvers.push(resolve))),
    setReadState: mocks.setReadState,
    respond: vi.fn(),
    openExternal: vi.fn(async () => ({ ok: true })),
  },
}));

import App from "@/App.vue";

beforeEach(() => {
  vi.clearAllMocks();
  mocks.updateResolvers.splice(0);
});
afterEach(() => vi.restoreAllMocks());

describe("large feed", () => {
  it("renders bounded windows, grows on request, and mutates explicit current-view ids", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.findAll("article")).toHaveLength(50);
    const feed = wrapper.get("main").element as HTMLElement;
    feed.scrollTop = 400;
    await wrapper.get(".show-older").trigger("click");
    expect(wrapper.findAll("article")).toHaveLength(100);
    expect(feed.scrollTop).toBe(400);
    const bulk = wrapper.findAll(".feed-toolbar button")[0];
    await bulk.trigger("click");
    await flushPromises();
    expect(mocks.setReadState).toHaveBeenCalledTimes(1);
    expect(mocks.setReadState.mock.calls[0][0]).toHaveLength(120);
    wrapper.unmount();
  });

  it("renders connection updates and disables mutations while offline", async () => {
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.get(".connection").text()).toBe("Connected to server");
    mocks.updateResolvers.shift()?.({
      revision: 2,
      updates: [{ kind: "connection", connection: { state: "offline", message: "Network unavailable" } }],
    });
    await flushPromises();

    expect(wrapper.get(".connection").text()).toBe("Network unavailable");
    expect(wrapper.get(".connection").attributes("data-state")).toBe("offline");
    expect(wrapper.findAll(".feed-toolbar button").every((button) => button.attributes("disabled") !== undefined)).toBe(true);
    wrapper.unmount();
  });

  it("shows a new-notification affordance without moving a reader away from the top", async () => {
    const wrapper = mount(App);
    await flushPromises();
    const feed = wrapper.get("main").element as HTMLElement;
    feed.scrollTop = 200;
    let anchorMeasurements = 0;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const top = this.dataset.notificationId === "119" && anchorMeasurements++ > 0 ? 180 : 100;
      return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top, toJSON: () => ({}) };
    });
    const newest = { ...mocks.items[0], id: "new", summary: "Just arrived", created_at: "2027-01-01T00:00:00.000Z" };
    mocks.updateResolvers.shift()?.({
      revision: 2,
      updates: [{ kind: "events", domains: [], events: [{ seq: 2, type: "notification.created", notification: newest }] }],
    });
    await flushPromises();

    expect(wrapper.get(".new-notifications").text()).toContain("return to top");
    expect(wrapper.findAll("article")).toHaveLength(50);
    expect(feed.scrollTop).toBe(280);
    wrapper.unmount();
  });
});
