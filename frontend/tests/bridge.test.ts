import { afterEach, describe, expect, it, vi } from "vitest";

import type { PywebviewApi } from "@/api/bridge";
import { desktopBridge, waitForPywebviewApi } from "@/api/bridge";
import { fakePywebviewApi } from "@/api/fakeBridge";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete window.pywebview;
});

describe("desktop bridge readiness", () => {
  it("waits for methods when pywebview initially exposes an empty API placeholder", async () => {
    vi.useFakeTimers();
    const placeholder = {} as PywebviewApi;
    window.pywebview = { api: placeholder };

    const ready = waitForPywebviewApi();
    window.dispatchEvent(new Event("pywebviewready"));
    Object.assign(placeholder, {
      get_initial_state: vi.fn(),
      get_updates: vi.fn(),
      set_read_state: vi.fn(),
      respond: vi.fn(),
      open_external: vi.fn(),
      get_settings: vi.fn(),
      update_settings: vi.fn(),
      choose_sound_file: vi.fn(),
      resolve_sound_path: vi.fn(),
    });
    await vi.advanceTimersByTimeAsync(25);

    await expect(ready).resolves.toBe(placeholder);
  });

  it("reports a bridge that never becomes ready", async () => {
    vi.useFakeTimers();
    window.pywebview = { api: {} as PywebviewApi };

    const ready = waitForPywebviewApi(100);
    const rejection = expect(ready).rejects.toThrow("did not become ready");
    await vi.advanceTimersByTimeAsync(100);

    await rejection;
  });

  it("rejects malformed settings responses at the bridge boundary", async () => {
    vi.spyOn(fakePywebviewApi, "get_settings").mockResolvedValue({
      ok: true,
      settings: { theme: "purple" },
    });

    await expect(desktopBridge.getSettings()).rejects.toThrow("invalid settings data");
  });
});
