import { afterEach, describe, expect, it, vi } from "vitest";

import type { Notification, UpdateBatch } from "@/model/protocol";
import { NotificationSoundService, type SoundPathResolver, type SoundPlayer } from "@/presentation/sound";

function notification(pending = false): Notification {
  return {
    id: crypto.randomUUID(), domain: "tests", sender: "tests", summary: "New", message: "", details: null,
    tags: [], priority: "normal", source_created_at: null, created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z", read_at: null,
    response_state: pending ? "pending" : "not_requested", response_options: [], response: null, version: 1,
  };
}

function batch(...events: Array<{ seq: number; pending?: boolean }>): UpdateBatch {
  return {
    revision: 2,
    updates: [{ kind: "events", domains: [], events: events.map(({ seq, pending }) => ({
      seq, type: "notification.created" as const, notification: notification(pending),
    })) }],
  };
}

function setup(resolve: SoundPathResolver = vi.fn(async () => ({ ok: true as const, path: "/tone.wav", uri: "file:///tone.wav" }))) {
  const player: SoundPlayer = { playBundled: vi.fn(async () => undefined), playUri: vi.fn(async () => undefined) };
  const report = vi.fn();
  const service = new NotificationSoundService(player, resolve, report, 100);
  service.observeInitial(10);
  return { service, player, resolve, report };
}

afterEach(() => vi.useRealTimers());

describe("notification sound policy", () => {
  it.each([
    ["never", false, 0], ["response_required", false, 0], ["response_required", true, 1], ["all", false, 1],
  ] as const)("applies %s mode", async (sound, pending, count) => {
    vi.useFakeTimers();
    const { service, player } = setup();
    service.configure({ sound, response_required_sound_path: "", informational_sound_path: "" });
    service.inspect(batch({ seq: 11, pending }));
    await vi.advanceTimersByTimeAsync(100);
    expect(player.playBundled).toHaveBeenCalledTimes(count);
  });

  it("coalesces mixed bursts and lets response-required win", async () => {
    vi.useFakeTimers();
    const { service, player } = setup();
    service.configure({ sound: "all", response_required_sound_path: "", informational_sound_path: "" });
    service.inspect(batch({ seq: 11 }, { seq: 12, pending: true }, { seq: 13 }));
    await vi.advanceTimersByTimeAsync(100);
    expect(player.playBundled).toHaveBeenCalledOnce();
    expect(player.playBundled).toHaveBeenCalledWith("response_required");
  });

  it("keeps startup, reset, and reconnect replay silent", async () => {
    vi.useFakeTimers();
    const { service, player } = setup();
    service.configure({ sound: "all", response_required_sound_path: "", informational_sound_path: "" });
    service.inspect(batch({ seq: 9 }, { seq: 10 }));
    service.inspect({ revision: 3, updates: [{ kind: "reset", snapshot: { sequence: 20, domains: [], notifications: [] } }] });
    service.inspect(batch({ seq: 19 }, { seq: 20 }));
    await vi.runAllTimersAsync();
    expect(player.playBundled).not.toHaveBeenCalled();
  });

  it("does not sound until a snapshot has completed", async () => {
    vi.useFakeTimers();
    const player: SoundPlayer = { playBundled: vi.fn(async () => undefined), playUri: vi.fn(async () => undefined) };
    const service = new NotificationSoundService(player, vi.fn(), vi.fn(), 100);
    service.configure({ sound: "all", response_required_sound_path: "", informational_sound_path: "" });
    service.inspect(batch({ seq: 1 }));
    service.inspect({ revision: 3, updates: [{ kind: "reset", snapshot: { sequence: 2, domains: [], notifications: [] } }] });
    service.inspect(batch({ seq: 3 }));
    await vi.runAllTimersAsync();
    expect(player.playBundled).toHaveBeenCalledOnce();
  });

  it("uses normalized custom paths and falls back with a visible error", async () => {
    vi.useFakeTimers();
    const valid = setup();
    valid.service.configure({ sound: "all", response_required_sound_path: "", informational_sound_path: "~/tone.wav" });
    valid.service.inspect(batch({ seq: 11 }));
    await vi.runAllTimersAsync();
    expect(valid.resolve).toHaveBeenCalledWith("~/tone.wav");
    expect(valid.player.playUri).toHaveBeenCalledWith("file:///tone.wav");

    const invalid = setup(vi.fn(async () => ({ ok: false as const, error: { message: "Missing; using bundled." } })));
    invalid.service.configure({ sound: "all", response_required_sound_path: "", informational_sound_path: "/missing.wav" });
    invalid.service.inspect(batch({ seq: 11 }));
    await vi.runAllTimersAsync();
    expect(invalid.report).toHaveBeenLastCalledWith("Missing; using bundled.");
    expect(invalid.player.playBundled).toHaveBeenCalledOnce();
  });

  it("uses a separate path for each type and generates the other type when its path is empty", async () => {
    vi.useFakeTimers();
    const { service, player, resolve } = setup(vi.fn(async (path: string) => ({ ok: true as const, path, uri: `file://${path}` })));
    service.configure({ sound: "all", response_required_sound_path: "/action.wav", informational_sound_path: "" });
    service.inspect(batch({ seq: 11 }));
    await vi.runAllTimersAsync();
    expect(player.playBundled).toHaveBeenCalledWith("informational");
    expect(resolve).not.toHaveBeenCalled();

    service.inspect(batch({ seq: 12, pending: true }));
    await vi.runAllTimersAsync();
    expect(resolve).toHaveBeenCalledWith("/action.wav");
    expect(player.playUri).toHaveBeenCalledWith("file:///action.wav");

    service.configure({ sound: "all", response_required_sound_path: "", informational_sound_path: "/info.wav" });
    service.inspect(batch({ seq: 13, pending: true }));
    await vi.runAllTimersAsync();
    expect(player.playBundled).toHaveBeenCalledWith("response_required");
    service.inspect(batch({ seq: 14 }));
    await vi.runAllTimersAsync();
    expect(resolve).toHaveBeenLastCalledWith("/info.wav");
  });

  it("applies path changes immediately, cancels queued sound for never, and ignores rejection", async () => {
    vi.useFakeTimers();
    const { service, player, resolve } = setup();
    vi.mocked(player.playUri).mockRejectedValueOnce(new Error("autoplay denied"));
    service.configure({ sound: "all", response_required_sound_path: "", informational_sound_path: "/old.wav" });
    service.inspect(batch({ seq: 11 }));
    service.configure({ sound: "all", response_required_sound_path: "", informational_sound_path: "/new.wav" });
    await vi.runAllTimersAsync();
    expect(resolve).toHaveBeenCalledWith("/new.wav");
    expect(player.playBundled).toHaveBeenCalledWith("informational");

    service.inspect(batch({ seq: 12 }));
    service.configure({ sound: "never", response_required_sound_path: "", informational_sound_path: "/new.wav" });
    await vi.runAllTimersAsync();
    expect(resolve).toHaveBeenCalledTimes(1);
  });
});
