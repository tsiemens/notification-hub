export type ConnectionState = "starting" | "connected" | "reconnecting" | "offline" | "fatal";

export interface ConnectionStatus {
  state: ConnectionState;
  message: string | null;
}

export interface ResponseOption {
  id: string;
  label: string;
  message_mode: "none" | "optional" | "required";
  appearance: "default" | "primary" | "danger";
}

export interface Notification {
  id: string;
  domain: string;
  sender: string;
  summary: string;
  message: string;
  details: string | null;
  tags: string[];
  priority: "low" | "normal" | "high" | "urgent";
  source_created_at: string | null;
  created_at: string;
  updated_at: string;
  read_at: string | null;
  response_state: "not_requested" | "pending" | "answered" | "cancelled" | "expired";
  response_options: ResponseOption[];
  response: {
    request_id: string;
    option_id: string;
    message: string | null;
    responded_at: string;
    responded_by: string;
  } | null;
  version: number;
}

export interface DomainSummary {
  name: string;
  last_activity_at: string;
  notification_count: number;
  unread_count: number;
  pending_response_count: number;
  latest_summary: string;
}

export interface Snapshot {
  sequence: number;
  domains: DomainSummary[];
  notifications: Notification[];
}

export type HubEvent =
  | { seq: number; type: "notification.created" | "notification.updated"; notification: Notification }
  | { seq: number; type: "notifications.read_state_changed"; notification_ids: string[]; versions: number[]; read_at: string | null }
  | { seq: number; type: "notification.deleted"; id: string }
  | { seq: number; type: "domain.deleted"; name: string };

export type BridgeUpdate =
  | { kind: "connection"; connection: ConnectionStatus }
  | { kind: "events"; events: HubEvent[]; domains: DomainSummary[] }
  | { kind: "reset"; snapshot: Snapshot };

export interface InitialState {
  revision: number;
  connection: ConnectionStatus;
  snapshot: Snapshot | null;
}

export interface UpdateBatch {
  revision: number;
  updates: BridgeUpdate[];
}

export interface MutationError {
  code: string;
  message: string;
  retryable: boolean;
  notification?: Notification;
}

export type MutationResult =
  | { ok: true; notification?: Notification; notifications?: Notification[]; event_seq?: number | null }
  | { ok: false; error: MutationError };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function assertInitialState(value: unknown): asserts value is InitialState {
  if (!isRecord(value) || !Number.isInteger(value.revision) || !isRecord(value.connection)) {
    throw new Error("The desktop bridge returned an invalid initial state.");
  }
}

export function assertUpdateBatch(value: unknown): asserts value is UpdateBatch {
  if (!isRecord(value) || !Number.isInteger(value.revision) || !Array.isArray(value.updates)) {
    throw new Error("The desktop bridge returned an invalid update batch.");
  }
  for (const update of value.updates) {
    if (!isRecord(update) || !["connection", "events", "reset"].includes(String(update.kind))) {
      throw new Error("The desktop bridge returned an unknown update.");
    }
  }
}
