import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import MarkdownContent from "@/components/MarkdownContent.vue";
import type { DesktopBridge } from "@/api/bridge";

function bridge(): DesktopBridge {
  return {
    getInitialState: vi.fn(),
    getUpdates: vi.fn(),
    setReadState: vi.fn(),
    respond: vi.fn(),
    openExternal: vi.fn(async () => ({ ok: true })),
    getSettings: vi.fn(),
    updateSettings: vi.fn(),
  };
}

describe("hardened Markdown", () => {
  it("drops raw HTML, executable attributes, images, and unsafe links", () => {
    const wrapper = mount(MarkdownContent, {
      props: {
        bridge: bridge(),
        label: "message",
        source: "<script>window.pywebview.api.respond()</script><img src=x onerror=alert(1)>\n[bad](javascript:alert(1)) [data](data:text/html,x)",
      },
    });

    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.html()).not.toContain("onerror");
    expect(wrapper.findAll("a[href]")).toHaveLength(0);
  });

  it("delegates absolute HTTP links without navigating the webview", async () => {
    const api = bridge();
    const wrapper = mount(MarkdownContent, {
      props: { bridge: api, label: "message", source: "[safe](https://example.test/path) [relative](/inside)" },
    });
    const links = wrapper.findAll("a");
    expect(links[0].attributes("href")).toBeUndefined();
    expect(links[0].attributes("data-external-url")).toBe("https://example.test/path");
    expect(links[0].attributes("tabindex")).toBe("0");
    expect(links[1].attributes("href")).toBeUndefined();

    await links[0].trigger("click");
    expect(api.openExternal).toHaveBeenCalledWith("https://example.test/path");
    await links[0].trigger("keydown", { key: "Enter" });
    expect(api.openExternal).toHaveBeenCalledTimes(2);
  });

  it("shows hostile source as text in raw mode", async () => {
    const wrapper = mount(MarkdownContent, {
      props: { bridge: bridge(), label: "details", source: "<svg onload=alert(1)>" },
    });
    await wrapper.get("button").trigger("click");
    expect(wrapper.get("pre").text()).toBe("<svg onload=alert(1)>");
    expect(wrapper.find("svg").exists()).toBe(false);
  });

  it("uses the settings default until the user overrides the block for the session", async () => {
    const wrapper = mount(MarkdownContent, {
      props: { bridge: bridge(), label: "message", source: "**source**", initialRaw: true },
    });
    expect(wrapper.get("pre").text()).toBe("**source**");

    await wrapper.get("button").trigger("click");
    expect(wrapper.find("pre").exists()).toBe(false);
    await wrapper.setProps({ initialRaw: false });
    expect(wrapper.find("pre").exists()).toBe(false);
  });
});
