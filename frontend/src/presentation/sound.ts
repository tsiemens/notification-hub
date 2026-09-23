import type { UpdateBatch } from "@/model/protocol";
import type { ClientSettings } from "@/model/settings";

export type SoundOutcome = "informational" | "response_required";

export interface SoundPlayer {
  playBundled(outcome: SoundOutcome): Promise<void>;
  playUri(uri: string): Promise<void>;
}

export interface SoundPathResolver {
  (path: string): Promise<{ ok: true; path: string; uri: string } | { ok: false; error: { message: string } }>;
}

export class BrowserSoundPlayer implements SoundPlayer {
  async playBundled(outcome: SoundOutcome): Promise<void> {
    const Context = window.AudioContext
      ?? (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Context) return;
    const context = new Context();
    try {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.frequency.value = outcome === "response_required" ? 880 : 660;
      gain.gain.setValueAtTime(0.08, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.14);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + 0.15);
      await new Promise<void>((resolve) => { oscillator.onended = () => resolve(); });
    } finally {
      await context.close().catch(() => undefined);
    }
  }

  async playUri(uri: string): Promise<void> {
    await new Audio(uri).play();
  }
}

export class NotificationSoundService {
  private settings: Pick<ClientSettings, "sound" | "response_required_sound_path" | "informational_sound_path"> = { sound: "never", response_required_sound_path: "", informational_sound_path: "" };
  private highestSequence = 0;
  private initialSnapshotComplete = false;
  private queued: SoundOutcome | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly player: SoundPlayer,
    private readonly resolvePath: SoundPathResolver,
    private readonly reportError: (message: string | null) => void = () => undefined,
    private readonly debounceMs = 120,
  ) {}

  observeInitial(sequence: number): void {
    this.highestSequence = Math.max(this.highestSequence, sequence);
    this.initialSnapshotComplete = true;
  }

  configure(settings: Pick<ClientSettings, "sound" | "response_required_sound_path" | "informational_sound_path">): void {
    this.settings = settings;
    this.reportError(null);
    if (settings.sound === "never") this.cancel();
  }

  inspect(batch: UpdateBatch): void {
    let outcome: SoundOutcome | null = null;
    for (const update of batch.updates) {
      if (update.kind === "reset") {
        this.highestSequence = Math.max(this.highestSequence, update.snapshot.sequence);
        this.initialSnapshotComplete = true;
        continue;
      }
      if (update.kind !== "events") continue;
      for (const event of update.events) {
        const unseen = event.seq > this.highestSequence;
        this.highestSequence = Math.max(this.highestSequence, event.seq);
        if (!this.initialSnapshotComplete || !unseen || event.type !== "notification.created") continue;
        const candidate: SoundOutcome = event.notification.response_state === "pending"
          ? "response_required" : "informational";
        if (this.settings.sound === "all" || (this.settings.sound === "response_required" && candidate === "response_required")) {
          if (candidate === "response_required" || outcome === null) outcome = candidate;
        }
      }
    }
    if (outcome) this.enqueue(outcome);
  }

  stop(): void { this.cancel(); }

  private enqueue(outcome: SoundOutcome): void {
    if (outcome === "response_required" || this.queued === null) this.queued = outcome;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => { void this.flush(); }, this.debounceMs);
  }

  private cancel(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.queued = null;
  }

  private async flush(): Promise<void> {
    this.timer = null;
    const outcome = this.queued;
    this.queued = null;
    if (!outcome || this.settings.sound === "never") return;
    try {
      const path = outcome === "response_required" ? this.settings.response_required_sound_path : this.settings.informational_sound_path;
      if (path) {
        const resolved = await this.resolvePath(path);
        if (resolved.ok) {
          try {
            await this.player.playUri(resolved.uri);
          } catch {
            this.reportError("The custom sound could not be played; the bundled sound will be used.");
            await this.player.playBundled(outcome);
          }
          return;
        }
        this.reportError(resolved.error.message);
      }
      await this.player.playBundled(outcome);
    } catch {
      // Autoplay denial, missing audio support, and playback rejection are non-fatal.
    }
  }
}
