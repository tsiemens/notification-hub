import type { Theme } from "@/model/settings";

const DARK_QUERY = "(prefers-color-scheme: dark)";

export function resolvedTheme(theme: Theme, prefersDark: boolean): "light" | "dark" {
  return theme === "system" ? (prefersDark ? "dark" : "light") : theme;
}

function setThemeAttributes(theme: Theme, prefersDark: boolean): void {
  const root = document.documentElement;
  const resolved = resolvedTheme(theme, prefersDark);
  root.dataset.theme = resolved;
  root.dataset.themePreference = theme;
  root.style.colorScheme = resolved;
}

/** Establish a system-aware palette before Vue performs its first render. */
export function primeTheme(): void {
  const query = window.matchMedia?.(DARK_QUERY);
  setThemeAttributes("system", query?.matches ?? false);
}

/** Apply a saved preference and return a disposer for the system listener. */
export function installTheme(theme: Theme): () => void {
  const query = window.matchMedia?.(DARK_QUERY);
  const apply = (): void => setThemeAttributes(theme, query?.matches ?? false);
  apply();
  if (theme !== "system" || !query) return () => undefined;
  query.addEventListener("change", apply);
  return () => query.removeEventListener("change", apply);
}
