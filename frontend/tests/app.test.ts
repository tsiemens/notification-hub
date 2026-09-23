import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { InitialState, Notification } from "@/model/protocol";
import type { ClientSettings } from "@/model/settings";

const mocks = vi.hoisted(() => {
  const items: Notification[] = Array.from({ length: 120 }, (_, index) => ({
    id: String(index), domain: "large", sender: "tests", summary: `Notice ${index}`, message: "", details: null,
    tags: [], priority: "normal" as const, source_created_at: null,
    created_at: new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString(), read_at: null,
    response_state: "not_requested" as const, response_options: [], response: null, version: 1,
  }));
  const setReadState = vi.fn(async (ids: string[]) => ({ ok: true as const, notifications: ids.map((id) => ({ ...items[Number(id)], read_at: "2026-01-02T00:00:00.000Z", version: 2 })) }));
  const respond = vi.fn();
  const getSettings = vi.fn(async (): Promise<ClientSettings> => ({ theme: "system", sound: "response_required", sound_path: "", hide_read: false, raw_markdown: false, views: [] }));
  const getInitialState = vi.fn(async (): Promise<InitialState> => ({ revision: 1, connection: { state: "connected", message: null }, snapshot: {
    sequence: 1,
    domains: [{ name: "large", last_activity_at: items[119].created_at, notification_count: 120, unread_count: 120, pending_response_count: 0, latest_summary: "Notice 119" }],
    notifications: items,
  } }));
  const updateSettings = vi.fn(async (settings: unknown) => ({ ok: true as const, settings }));
  const updateResolvers: Array<(value: unknown) => void> = [];
  return { items, setReadState, respond, getSettings, getInitialState, updateSettings, updateResolvers };
});

vi.mock("@/api/bridge", () => ({
  desktopBridge: {
    getInitialState: mocks.getInitialState,
    getUpdates: vi.fn(() => new Promise((resolve) => mocks.updateResolvers.push(resolve))),
    setReadState: mocks.setReadState,
    respond: mocks.respond,
    openExternal: vi.fn(async () => ({ ok: true })),
    getSettings: mocks.getSettings,
    updateSettings: mocks.updateSettings,
    chooseSoundFile: vi.fn(async () => null),
    resolveSoundPath: vi.fn(async () => ({ ok: false, error: { message: "Unavailable" } })),
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
    expect(mocks.getSettings).toHaveBeenCalledOnce();
    expect(wrapper.get(".header-actions button").attributes("disabled")).toBeUndefined();
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
    expect(wrapper.get(".connection").attributes("aria-label")).toBe("Network unavailable");
    expect(wrapper.get(".connection-icon").attributes("aria-hidden")).toBe("true");
    expect(wrapper.get(".connection-popover").attributes("role")).toBe("tooltip");
    expect(wrapper.findAll(".feed-toolbar button").every((button) => button.attributes("disabled") !== undefined)).toBe(true);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "m" }));
    expect(mocks.setReadState).not.toHaveBeenCalled();
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

  it("renders accessible unread markers and marks exactly the filtered unread ids", async () => {
    const items = mocks.items.map((item, index) => index < 2
      ? { ...item, read_at: "2026-01-02T00:00:00.000Z" }
      : item);
    mocks.getInitialState.mockResolvedValueOnce({
      revision: 1,
      connection: { state: "connected", message: null },
      snapshot: {
        sequence: 1,
        domains: [{ name: "large", last_activity_at: items[119].created_at, notification_count: 120, unread_count: 118, pending_response_count: 0, latest_summary: "Notice 119" }],
        notifications: items,
      },
    });
    mocks.getSettings.mockResolvedValueOnce({
      theme: "system", sound: "never", sound_path: "", hide_read: false, raw_markdown: false,
      views: [{ id: "tests", name: "Test sender", rules: [{ sender_regex: "^TESTS$" }] }],
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const wrapper = mount(App);
    await flushPromises();

    const markers = wrapper.findAll(".unread-marker");
    expect(markers).toHaveLength(3);
    expect(wrapper.findAll(".view-icon")).toHaveLength(1);
    expect(wrapper.get(".view-icon").attributes("aria-hidden")).toBe("true");
    expect(markers.every((marker) => marker.text().includes("Unread"))).toBe(true);
    expect(markers[1].attributes("aria-label")).toBe("118 unread notifications");
    (wrapper.get("main").element as HTMLElement).scrollTo = vi.fn();
    await wrapper.findAll("nav button")[1].trigger("click");
    await wrapper.get(".feed-toolbar button").trigger("click");
    await flushPromises();

    expect(mocks.setReadState).toHaveBeenCalledWith(items.slice(2).reverse().map((item) => item.id), true);
    wrapper.unmount();
  });

  it("resets the bounded window and scroll for selection and hide-read changes", async () => {
    const wrapper = mount(App);
    await flushPromises();
    const feed = wrapper.get("main").element as HTMLElement;
    const scrollTo = vi.fn();
    feed.scrollTo = scrollTo;
    await wrapper.get(".show-older").trigger("click");
    expect(wrapper.findAll("article")).toHaveLength(100);
    await wrapper.findAll("nav button")[1].trigger("click");
    await flushPromises();
    expect(wrapper.findAll("article")).toHaveLength(50);
    expect(scrollTo).toHaveBeenCalledWith({ top: 0 });

    await wrapper.get(".show-older").trigger("click");
    await wrapper.get(".header-actions button").trigger("click");
    await wrapper.get('.settings-dialog input[type="checkbox"]').setValue(true);
    await wrapper.get(".settings-dialog form").trigger("submit");
    await flushPromises();
    expect(wrapper.findAll("article")).toHaveLength(50);
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 0 });
    wrapper.unmount();
  });

  it("navigates within boundaries, reveals distant cards, and invokes explicit card actions", async () => {
    const wrapper = mount(App, { attachTo: document.body });
    await flushPromises();
    const selected = () => wrapper.get('article[aria-current="true"]');
    expect(selected().attributes("data-notification-id")).toBe("119");
    expect(selected().attributes("tabindex")).toBe("0");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k" }));
    await flushPromises();
    expect(selected().attributes("data-notification-id")).toBe("119");
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j" }));
    await flushPromises();
    expect(selected().attributes("data-notification-id")).toBe("118");
    expect(document.activeElement).toBe(selected().element);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "m" }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "m" }));
    expect(mocks.setReadState).toHaveBeenCalledOnce();
    expect(mocks.setReadState).toHaveBeenCalledWith(["118"], true);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Home" }));
    await flushPromises();
    expect(selected().attributes("data-notification-id")).toBe("119");
    wrapper.unmount();
  });

  it("reconciles selection when the selected notification is removed", async () => {
    const wrapper = mount(App);
    await flushPromises();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j" }));
    await flushPromises();
    expect(wrapper.get('article[aria-current="true"]').attributes("data-notification-id")).toBe("118");

    mocks.updateResolvers.shift()?.({
      revision: 2,
      updates: [{ kind: "events", domains: [], events: [{ seq: 2, type: "notification.deleted", id: "118" }] }],
    });
    await flushPromises();
    expect(wrapper.get('article[aria-current="true"]').attributes("data-notification-id")).toBe("117");
    wrapper.unmount();
  });

  it("selects the nearest remaining card when filtering hides the selection", async () => {
    const items = mocks.items.map((item) => item.id === "119"
      ? { ...item, read_at: "2026-01-02T00:00:00.000Z" }
      : item);
    mocks.getInitialState.mockResolvedValueOnce({
      revision: 1,
      connection: { state: "connected", message: null },
      snapshot: {
        sequence: 1,
        domains: [{ name: "large", last_activity_at: items[119].created_at, notification_count: 120, unread_count: 119, pending_response_count: 0, latest_summary: "Notice 119" }],
        notifications: items,
      },
    });
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.get('article[aria-current="true"]').attributes("data-notification-id")).toBe("119");
    await wrapper.findAll(".header-actions button")[0].trigger("click");
    await wrapper.get('.settings-dialog input[type="checkbox"]').setValue(true);
    await wrapper.get(".settings-dialog form").trigger("submit");
    await flushPromises();
    expect(wrapper.get('article[aria-current="true"]').attributes("data-notification-id")).toBe("118");
    wrapper.unmount();
  });

  it("guards shortcuts in editable, modified, and composing contexts", async () => {
    const wrapper = mount(App);
    await flushPromises();
    const input = document.createElement("input");
    document.body.append(input);
    input.focus();
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "j", bubbles: true }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j", ctrlKey: true }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j", isComposing: true }));
    window.dispatchEvent(new CompositionEvent("compositionstart"));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j" }));
    window.dispatchEvent(new CompositionEvent("compositionend"));
    await flushPromises();
    expect(wrapper.get('article[aria-current="true"]').attributes("data-notification-id")).toBe("119");
    expect(mocks.setReadState).not.toHaveBeenCalled();
    input.remove();
    wrapper.unmount();
  });

  it("toggles details with Enter and never turns an application shortcut into a response", async () => {
    const option = { id: "approve", label: "Approve", message_mode: "optional" as const, appearance: "primary" as const };
    const actionable = {
      ...mocks.items[119], details: "More information", response_state: "pending" as const,
      response_options: [option],
    };
    mocks.getInitialState.mockResolvedValueOnce({
      revision: 1,
      connection: { state: "connected", message: null },
      snapshot: {
        sequence: 1,
        domains: [{ name: "large", last_activity_at: actionable.created_at, notification_count: 1, unread_count: 1, pending_response_count: 1, latest_summary: actionable.summary }],
        notifications: [actionable],
      },
    });
    const wrapper = mount(App, { attachTo: document.body });
    await flushPromises();
    const card = wrapper.get('article[aria-current="true"]');
    card.element.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await flushPromises();
    expect(card.get(".details-toggle").attributes("aria-expanded")).toBe("true");
    expect(mocks.respond).not.toHaveBeenCalled();

    const editor = card.get("textarea");
    await editor.setValue("local draft");
    editor.element.dispatchEvent(new KeyboardEvent("keydown", { key: "j", bubbles: true }));
    editor.element.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(mocks.respond).not.toHaveBeenCalled();
    editor.element.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect((editor.element as HTMLTextAreaElement).value).toBe("");
    expect(mocks.respond).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("shows the complete modal key map, traps focus, closes with Escape, and restores focus", async () => {
    const wrapper = mount(App, { attachTo: document.body });
    await flushPromises();
    const card = wrapper.get('article[aria-current="true"]');
    (card.element as HTMLElement).focus();
    card.element.dispatchEvent(new KeyboardEvent("keydown", { key: "?", shiftKey: true, bubbles: true }));
    await flushPromises();

    const dialog = wrapper.get('.keyboard-help-dialog[role="dialog"][aria-modal="true"]');
    expect(dialog.text()).toContain("j / k");
    expect(dialog.text()).toContain("Enter");
    expect(dialog.text()).toContain("Escape");
    const close = dialog.get('button[aria-label="Close keyboard shortcuts"]');
    expect(document.activeElement).toBe(close.element);
    close.element.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(close.element);
    close.element.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(wrapper.find(".keyboard-help-dialog").exists()).toBe(false);
    expect(document.activeElement).toBe(card.element);
    wrapper.unmount();
  });
});
