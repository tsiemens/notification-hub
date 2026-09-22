import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import type { DesktopBridge } from "@/api/bridge";
import NotificationCard from "@/components/NotificationCard.vue";
import type { Notification, ResponseOption } from "@/model/protocol";

const api = {
  getInitialState: vi.fn(), getUpdates: vi.fn(), setReadState: vi.fn(), respond: vi.fn(),
  openExternal: vi.fn(async () => ({ ok: true })), getSettings: vi.fn(), updateSettings: vi.fn(),
} as DesktopBridge;

const modes: Record<ResponseOption["message_mode"], ResponseOption> = {
  none: { id: "skip", label: "Skip", message_mode: "none", appearance: "default" },
  optional: { id: "note", label: "Add note", message_mode: "optional", appearance: "default" },
  required: { id: "approve", label: "Approve", message_mode: "required", appearance: "primary" },
};

function item(responseOptions: ResponseOption[] = [modes.none, modes.required]): Notification {
  return {
    id: "a", domain: "ops", sender: "tests", summary: "Choose", message: "**Now**", details: "_More_",
    tags: ["deploy"], priority: "urgent", source_created_at: null, created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z", read_at: null, response_state: "pending",
    response_options: responseOptions, response: null, version: 1,
  };
}

function mountCard(notification = item(), extra: Record<string, unknown> = {}) {
  return mount(NotificationCard, {
    props: {
      notification, bridge: api, pending: false, connected: true,
      now: Date.parse("2026-01-01T02:00:00Z"), ...extra,
    },
  });
}

describe("notification card", () => {
  it("uses compact, explained card actions and emits read-state changes", async () => {
    const wrapper = mountCard();
    expect(wrapper.text()).toContain("2h ago");
    expect(wrapper.get(".read-toggle").attributes("aria-label")).toBe("Mark read");
    expect(wrapper.get(".read-toggle svg").attributes("aria-hidden")).toBe("true");
    expect(wrapper.get(".read-toggle + .action-tooltip").text()).toBe("Mark read");
    expect(wrapper.find(".card-actions .text-button").exists()).toBe(false);
    await wrapper.get(".read-toggle").trigger("click");
    expect(wrapper.emitted("read")?.[0]).toEqual([wrapper.props("notification"), true]);
  });

  it("uses one source toggle for the message and expanded details", async () => {
    const wrapper = mountCard(item(), { rawMarkdown: true });
    expect(wrapper.findAll(".source-toggle")).toHaveLength(1);
    expect(wrapper.get(".source-toggle").attributes("aria-pressed")).toBe("true");
    expect(wrapper.findAll("pre.raw-source")).toHaveLength(1);
    expect(wrapper.findAll(".raw-toggle")).toHaveLength(0);
    await wrapper.get(".details-toggle").trigger("click");
    expect(wrapper.get(".details-toggle").attributes("aria-expanded")).toBe("true");
    expect(wrapper.get(".details-toggle").attributes("aria-controls")).toBe("details-a");
    expect(wrapper.get("#details-a").text()).toContain("_More_");
    expect(wrapper.findAll("pre.raw-source")).toHaveLength(2);
    await wrapper.get(".source-toggle").trigger("click");
    expect(wrapper.get(".source-toggle").attributes("aria-label")).toBe("Show source content");
    expect(wrapper.findAll("pre.raw-source")).toHaveLength(0);
    expect(wrapper.findAll(".markdown")).toHaveLength(2);
  });

  it("renders an accessible details divider", () => {
    const disclosure = mountCard().get(".details-toggle");
    expect(disclosure.text()).toContain("Details");
    expect(disclosure.get(".disclosure-chevron").attributes("aria-hidden")).toBe("true");
    expect(disclosure.get(".details-divider").attributes("aria-hidden")).toBe("true");
    expect(disclosure.attributes("aria-expanded")).toBe("false");
  });

  it("omits the editor when every choice ignores messages", async () => {
    const wrapper = mountCard(item([modes.none, { ...modes.none, id: "later", label: "Later" }]));
    expect(wrapper.find("textarea").exists()).toBe(false);
    await wrapper.findAll(".response-buttons button")[0].trigger("click");
    expect(wrapper.emitted("respond")?.[0]?.slice(1)).toEqual([modes.none, null]);
  });

  it("puts one shared editor before mixed choices and communicates each mode", async () => {
    const wrapper = mountCard(item([modes.none, modes.optional, modes.required]));
    const editor = wrapper.get("textarea");
    const buttons = wrapper.findAll(".response-buttons button");
    expect(wrapper.get(".response-message-label").text()).toBe("Response Message");
    expect(editor.element.compareDocumentPosition(buttons[0].element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(buttons.map((button) => button.attributes("aria-label"))).toEqual([
      "Skip. Ignores the response message",
      "Add note. Uses the response message when provided",
      "Approve. Requires a response message; enter a message to enable this choice",
    ]);
    expect(buttons.every((button) => button.find(".response-mode-icon svg").exists())).toBe(true);
    expect(buttons[2].attributes("disabled")).toBeDefined();
    await editor.setValue("ship it");
    expect(buttons[2].attributes("disabled")).toBeUndefined();
    expect(buttons[2].attributes("aria-label")).toContain("Requires and sends");
  });

  it("derives payloads from each option's message mode", async () => {
    const wrapper = mountCard(item([modes.none, modes.optional, modes.required]));
    const editor = wrapper.get("textarea");
    const buttons = wrapper.findAll(".response-buttons button");
    await buttons[1].trigger("click");
    expect(wrapper.emitted("respond")?.[0]?.slice(1)).toEqual([modes.optional, null]);
    await editor.setValue("shared message");
    await buttons[0].trigger("click");
    await buttons[1].trigger("click");
    await buttons[2].trigger("click");
    expect(wrapper.emitted("respond")?.slice(1).map((event) => event.slice(1))).toEqual([
      [modes.none, null], [modes.optional, "shared message"], [modes.required, "shared message"],
    ]);
  });

  it("disables all mutations and the shared field while pending or offline", () => {
    for (const props of [{ pending: true, connected: true }, { pending: false, connected: false }]) {
      const wrapper = mountCard(item([modes.optional, modes.required]), props);
      expect(wrapper.get("textarea").attributes("disabled")).toBeDefined();
      expect(wrapper.get(".read-toggle").attributes("disabled")).toBeDefined();
      expect(wrapper.findAll(".response-buttons button").every((button) => button.attributes("disabled") !== undefined)).toBe(true);
      if (props.pending) expect(wrapper.text()).toContain("Submitting");
    }
  });

  it("preserves read styling, identity accents, and retry errors", () => {
    const unread = mountCard(item(), { error: { message: "Try later", retryable: true } });
    expect(unread.classes()).toContain("unread");
    expect(unread.get(".domain-label").attributes("data-identity-accent")).toMatch(/^(?:[0-9]|1[01])$/);
    expect(unread.get(".sender-label").attributes("data-identity-accent")).toMatch(/^(?:[0-9]|1[01])$/);
    expect(unread.text()).toContain("You can retry");
    const read = mountCard({ ...item(), read_at: "2026-01-02T00:00:00Z" });
    expect(read.classes()).not.toContain("unread");
    expect(read.get(".read-toggle").attributes("aria-label")).toBe("Mark unread");
  });

  it("separates answered metadata from the selected answer", () => {
    const answered = mountCard({
      ...item(),
      response_state: "answered",
      response: {
        request_id: "request-a", option_id: "approve", message: null,
        responded_at: "2026-09-22T15:56:22.538Z", responded_by: "demo-desktop-ui",
      },
    });
    expect(answered.get(".terminal-response strong").text()).toBe("Answered: Approve");
    expect(answered.get(".terminal-response small").text()).toContain("by demo-desktop-ui");
  });
});
