export const MAX_CUSTOM_VIEWS = 64;
export const MAX_VIEW_RULES = 32;
export const MAX_VIEW_REGEX_LENGTH = 1024;

export type Theme = "light" | "dark" | "system";
export type SoundPolicy = "never" | "response_required" | "all";

export interface ViewRule {
  domain_regex?: string;
  sender_regex?: string;
  tag_regex?: string;
}

export interface CustomView {
  id: string;
  name: string;
  rules: ViewRule[];
}

export interface ClientSettings {
  theme: Theme;
  sound: SoundPolicy;
  sound_path: string;
  hide_read: boolean;
  raw_markdown: boolean;
  views: CustomView[];
}

export interface SettingsError {
  code: string;
  message: string;
  retryable: boolean;
}

export type SettingsResult =
  | { ok: true; settings: ClientSettings }
  | { ok: false; error: SettingsError };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function invalid(): never {
  throw new Error("The desktop bridge returned invalid settings data.");
}

function hasOnly(value: Record<string, unknown>, keys: string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function readRule(value: unknown): ViewRule {
  if (!isRecord(value)) invalid();
  const allowed = ["domain_regex", "sender_regex", "tag_regex"];
  if (Object.keys(value).some((key) => !allowed.includes(key))) invalid();
  const result: ViewRule = {};
  for (const key of allowed as (keyof ViewRule)[]) {
    const expression = value[key];
    if (expression !== undefined) {
      if (typeof expression !== "string" || !expression || expression.length > MAX_VIEW_REGEX_LENGTH) invalid();
      result[key] = expression;
    }
  }
  if (!Object.keys(result).length) invalid();
  return result;
}

export function readSettings(value: unknown): ClientSettings {
  if (!isRecord(value)) invalid();
  if (!hasOnly(value, ["theme", "sound", "sound_path", "hide_read", "raw_markdown", "views"])) invalid();
  const { theme, sound, sound_path, hide_read, raw_markdown, views } = value;
  if (!(["light", "dark", "system"] as unknown[]).includes(theme)) invalid();
  if (!(["never", "response_required", "all"] as unknown[]).includes(sound)) invalid();
  if (typeof sound_path !== "string" || sound_path.includes("\0")) invalid();
  if (typeof hide_read !== "boolean" || typeof raw_markdown !== "boolean" || !Array.isArray(views)) invalid();
  if (views.length > MAX_CUSTOM_VIEWS) invalid();
  const ids = new Set<string>();
  const parsedViews = views.map((view): CustomView => {
    if (!isRecord(view) || typeof view.id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(view.id)) invalid();
    if (!hasOnly(view, ["id", "name", "rules"])) invalid();
    if (ids.has(view.id)) invalid();
    ids.add(view.id);
    if (typeof view.name !== "string" || view.name.length < 1 || view.name.length > 80 || !Array.isArray(view.rules)) invalid();
    if (!view.rules.length || view.rules.length > MAX_VIEW_RULES) invalid();
    return { id: view.id, name: view.name, rules: view.rules.map(readRule) };
  });
  return { theme: theme as Theme, sound: sound as SoundPolicy, sound_path, hide_read, raw_markdown, views: parsedViews };
}

export function readSettingsResult(value: unknown): SettingsResult {
  if (!isRecord(value) || typeof value.ok !== "boolean") invalid();
  if (value.ok) {
    if (!hasOnly(value, ["ok", "settings"])) invalid();
    return { ok: true, settings: readSettings(value.settings) };
  }
  if (!hasOnly(value, ["ok", "error"])) invalid();
  if (!isRecord(value.error) || typeof value.error.code !== "string" || typeof value.error.message !== "string" || typeof value.error.retryable !== "boolean") invalid();
  if (!hasOnly(value.error, ["code", "message", "retryable"])) invalid();
  return {
    ok: false,
    error: { code: value.error.code, message: value.error.message, retryable: value.error.retryable },
  };
}
