import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import type { DesktopBridge } from "@/api/bridge";
import SettingsDialog from "@/components/SettingsDialog.vue";
import { readSettingsResult, type ClientSettings } from "@/model/settings";

const original: ClientSettings = {
  theme: "system",
  sound: "response_required",
  hide_read: false,
  raw_markdown: false,
  views: [{ id: "ops", name: "Operations", rules: [{ domain_regex: "^ops" }] }],
};

function bridge(updateSettings = vi.fn()): DesktopBridge {
  return {
    getInitialState: vi.fn(),
    getUpdates: vi.fn(),
    setReadState: vi.fn(),
    respond: vi.fn(),
    openExternal: vi.fn(),
    getSettings: vi.fn(),
    updateSettings,
  };
}

describe("settings contract", () => {
  it("strictly validates settings response values", () => {
    expect(readSettingsResult({ ok: true, settings: original })).toEqual({ ok: true, settings: original });
    expect(() => readSettingsResult({ ok: true, settings: { ...original, hide_read: 0 } })).toThrow("invalid settings data");
    expect(() => readSettingsResult({ ok: true, settings: { ...original, private_key_file: "/secret" } })).toThrow("invalid settings data");
    expect(() => readSettingsResult({ ok: true, settings: { ...original, views: [{ ...original.views[0], rules: [{}] }] } })).toThrow("invalid settings data");
    expect(() => readSettingsResult({ ok: false, error: { code: "failed", message: "No", retryable: "yes" } })).toThrow("invalid settings data");
  });

  it("keeps edits in a draft and discards them on cancel", async () => {
    const wrapper = mount(SettingsDialog, { props: { settings: original, bridge: bridge() } });
    await wrapper.get('select').setValue("dark");
    await wrapper.get('input[type="checkbox"]').setValue(true);
    await wrapper.get('button[aria-label="Close settings"]').trigger("click");

    expect(wrapper.emitted("close")).toHaveLength(1);
    expect(original.theme).toBe("system");
    expect(original.hide_read).toBe(false);
  });

  it("adopts only the canonical settings returned after a successful save", async () => {
    const canonical = { ...original, theme: "dark" as const, views: [] };
    const update = vi.fn(async () => ({ ok: true as const, settings: canonical }));
    const wrapper = mount(SettingsDialog, { props: { settings: original, bridge: bridge(update) } });
    await wrapper.get('select').setValue("dark");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(update).toHaveBeenCalledWith(expect.objectContaining({ theme: "dark" }));
    expect(wrapper.emitted("saved")?.[0]).toEqual([canonical]);
  });

  it("shows a rejected save and leaves the dialog open", async () => {
    const update = vi.fn(async () => ({
      ok: false as const,
      error: { code: "settings_write_failed", message: "Disk is read-only.", retryable: true },
    }));
    const wrapper = mount(SettingsDialog, { props: { settings: original, bridge: bridge(update) } });
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.get('[role="alert"]').text()).toBe("Disk is read-only.");
    expect(wrapper.emitted("saved")).toBeUndefined();
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true);
  });

  it("supports adding, removing, and reordering views and rules", async () => {
    const wrapper = mount(SettingsDialog, { props: { settings: original, bridge: bridge() } });
    await wrapper.get(".section-heading button").trigger("click");
    expect(wrapper.findAll(".view-editor")).toHaveLength(2);
    const addRule = wrapper.findAll(".view-editor")[0].findAll("button").find((button) => button.text() === "Add rule");
    await addRule!.trigger("click");
    expect(wrapper.findAll(".rule-editor")).toHaveLength(3);
    await wrapper.get('button[aria-label="Move Operations down"]').trigger("click");
    expect((wrapper.findAll(".view-editor")[1].get("input").element as HTMLInputElement).value).toBe("Operations");
    await wrapper.get('button[aria-label="Remove Operations"]').trigger("click");
    expect(wrapper.findAll(".view-editor")).toHaveLength(1);
  });
});
