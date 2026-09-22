import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import type { DesktopBridge } from "@/api/bridge";
import NotificationCard from "@/components/NotificationCard.vue";
import type { Notification } from "@/model/protocol";

const api = {
  getInitialState: vi.fn(), getUpdates: vi.fn(), setReadState: vi.fn(), respond: vi.fn(),
  openExternal: vi.fn(async () => ({ ok: true })),
} as DesktopBridge;

function item(): Notification {
  return {
    id: "a", domain: "ops", sender: "tests", summary: "Choose", message: "**Now**", details: "More",
    tags: ["deploy"], priority: "urgent", source_created_at: null, created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z", read_at: null, response_state: "pending",
    response_options: [
      { id: "skip", label: "Skip", message_mode: "none", appearance: "default" },
      { id: "approve", label: "Approve", message_mode: "required", appearance: "primary" },
    ],
    response: null, version: 1,
  };
}

describe("notification card", () => {
  it("renders metadata, expands details, and emits direct responses", async () => {
    const wrapper = mount(NotificationCard, { props: { notification: item(), bridge: api, pending: false, connected: true, now: Date.parse("2026-01-01T02:00:00Z") } });
    expect(wrapper.text()).toContain("urgent");
    expect(wrapper.text()).toContain("2h ago");
    expect(wrapper.text()).toContain("Unread");
    await wrapper.get(".details-toggle").trigger("click");
    expect(wrapper.text()).toContain("More");
    await wrapper.findAll(".response-buttons button")[0].trigger("click");
    expect(wrapper.emitted("respond")?.[0]?.slice(1)).toEqual([item().response_options[0], null]);
  });

  it("validates required messages and preserves focusable retry controls", async () => {
    const wrapper = mount(NotificationCard, { props: { notification: item(), bridge: api, pending: false, connected: true, now: Date.now(), error: { message: "Try later", retryable: true } } });
    await wrapper.findAll(".response-buttons button")[1].trigger("click");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.text()).toContain("message is required");
    await wrapper.get("textarea").setValue("ship it");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("respond")?.[0]?.[2]).toBe("ship it");
    expect(wrapper.text()).toContain("You can retry");
  });

  it("disables mutations while disconnected or pending", () => {
    const wrapper = mount(NotificationCard, { props: { notification: item(), bridge: api, pending: true, connected: false, now: Date.now() } });
    expect(wrapper.findAll("button").filter((button) => button.text().includes("Mark") || button.text() === "Skip").every((button) => button.attributes("disabled") !== undefined)).toBe(true);
    expect(wrapper.text()).toContain("Submitting");
  });
});
