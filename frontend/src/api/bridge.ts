import type { InitialState, MutationResult, UpdateBatch } from "@/model/protocol";
import { assertInitialState, assertUpdateBatch } from "@/model/protocol";
import { fakePywebviewApi } from "@/api/fakeBridge";
import type { ClientSettings, SettingsResult } from "@/model/settings";
import { readSettingsResult } from "@/model/settings";

export interface PywebviewApi {
  get_initial_state(): Promise<unknown>;
  get_updates(afterRevision: number): Promise<unknown>;
  set_read_state(notificationIds: string[], read: boolean): Promise<MutationResult>;
  respond(notificationId: string, optionId: string, message: string | null): Promise<MutationResult>;
  open_external(url: string): Promise<{ ok: boolean }>;
  get_settings(): Promise<unknown>;
  update_settings(settings: ClientSettings): Promise<unknown>;
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi };
  }
}

export interface DesktopBridge {
  getInitialState(): Promise<InitialState>;
  getUpdates(afterRevision: number): Promise<UpdateBatch>;
  setReadState(notificationIds: string[], read: boolean): Promise<MutationResult>;
  respond(notificationId: string, optionId: string, message: string | null): Promise<MutationResult>;
  openExternal(url: string): Promise<{ ok: boolean }>;
  getSettings(): Promise<ClientSettings>;
  updateSettings(settings: ClientSettings): Promise<SettingsResult>;
}

async function pywebviewApi(): Promise<PywebviewApi> {
  if (import.meta.env.DEV) return fakePywebviewApi;
  return waitForPywebviewApi();
}

const API_METHODS: (keyof PywebviewApi)[] = [
  "get_initial_state",
  "get_updates",
  "set_read_state",
  "respond",
  "open_external",
  "get_settings",
  "update_settings",
];

function readyApi(): PywebviewApi | null {
  const api = window.pywebview?.api;
  return api && API_METHODS.every((method) => typeof api[method] === "function") ? api : null;
}

export function waitForPywebviewApi(timeoutMs = 10_000): Promise<PywebviewApi> {
  const current = readyApi();
  if (current) return Promise.resolve(current);

  return new Promise((resolve, reject) => {
    let intervalId = 0;
    let timeoutId = 0;

    const cleanup = () => {
      window.removeEventListener("pywebviewready", check);
      window.clearInterval(intervalId);
      window.clearTimeout(timeoutId);
    };
    const check = () => {
      const api = readyApi();
      if (!api) return;
      cleanup();
      resolve(api);
    };

    window.addEventListener("pywebviewready", check);
    // Some webview runtimes expose an empty API placeholder before firing the
    // ready event. Poll as well so a missed or early event cannot stall startup.
    intervalId = window.setInterval(check, 25);
    timeoutId = window.setTimeout(() => {
      cleanup();
      reject(new Error("The desktop bridge did not become ready."));
    }, timeoutMs);
    check();
  });
}

export const desktopBridge: DesktopBridge = {
  async getInitialState() {
    const value = await (await pywebviewApi()).get_initial_state();
    assertInitialState(value);
    return value;
  },
  async getUpdates(afterRevision) {
    const value = await (await pywebviewApi()).get_updates(afterRevision);
    assertUpdateBatch(value);
    return value;
  },
  async setReadState(notificationIds, read) {
    return (await pywebviewApi()).set_read_state(notificationIds, read);
  },
  async respond(notificationId, optionId, message) {
    return (await pywebviewApi()).respond(notificationId, optionId, message);
  },
  async openExternal(url) {
    return (await pywebviewApi()).open_external(url);
  },
  async getSettings() {
    const result = readSettingsResult(await (await pywebviewApi()).get_settings());
    if (!result.ok) throw new Error(result.error.message);
    return result.settings;
  },
  async updateSettings(settings) {
    return readSettingsResult(await (await pywebviewApi()).update_settings(settings));
  },
};
