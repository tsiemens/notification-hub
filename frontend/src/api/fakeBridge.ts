import type { PywebviewApi } from "@/api/bridge";
import type { InitialState, MutationResult } from "@/model/protocol";
import type { ClientSettings } from "@/model/settings";

let settings: ClientSettings = {
  theme: "system",
  sound: "response_required",
  hide_read: false,
  raw_markdown: false,
  views: [{ id: "development", name: "Development", rules: [{ tag_regex: "^development$" }] }],
};

const initial: InitialState = {
  revision: 1,
  connection: { state: "connected", message: null },
  snapshot: {
    sequence: 12,
    domains: [
      {
        name: "development",
        last_activity_at: "2026-09-21T12:00:00.000Z",
        notification_count: 1,
        unread_count: 1,
        pending_response_count: 0,
        latest_summary: "Frontend development mode",
      },
    ],
    notifications: [
      {
        id: "00000000-0000-4000-8000-000000000001",
        domain: "development",
        sender: "fake-bridge",
        summary: "Frontend development mode",
        message: "This notification comes from the browser-only fake bridge.",
        details: null,
        tags: ["development"],
        priority: "normal",
        source_created_at: null,
        created_at: "2026-09-21T12:00:00.000Z",
        updated_at: "2026-09-21T12:00:00.000Z",
        read_at: null,
        response_state: "not_requested",
        response_options: [],
        response: null,
        version: 1,
      },
    ],
  },
};

const unavailable: MutationResult = {
  ok: false,
  error: {
    code: "development_only",
    message: "Mutations are disabled in browser-only development mode.",
    retryable: false,
  },
};

export const fakePywebviewApi: PywebviewApi = {
  async get_initial_state() {
    return structuredClone(initial);
  },
  async get_updates(afterRevision) {
    await new Promise((resolve) => window.setTimeout(resolve, 500));
    return { revision: Math.max(afterRevision, initial.revision), updates: [] };
  },
  async set_read_state() {
    return unavailable;
  },
  async respond() {
    return unavailable;
  },
  async open_external() {
    return { ok: true };
  },
  async get_settings() {
    return { ok: true, settings: structuredClone(settings) };
  },
  async update_settings(updated) {
    settings = structuredClone(updated);
    return { ok: true, settings: structuredClone(settings) };
  },
};
