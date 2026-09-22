import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { IDENTITY_ACCENT_COUNT, identityAccentIndex } from "@/presentation/identity";
import { installTheme, resolvedTheme } from "@/presentation/theme";
const styles = readFileSync(resolve(process.cwd(), "src/styles/base.css"), "utf8");

function cssToken(selector: string, name: string): string {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const block = styles.match(new RegExp(`${escapedSelector}\\s*\\{([^}]+)`))?.[1];
  const value = block?.match(new RegExp(`--${name}:\\s*(#[0-9a-f]+)`))?.[1];
  if (!value) throw new Error(`Missing ${name} in ${selector}`);
  return value;
}

function relativeLuminance(hex: string): number {
  const full = hex.length === 4 ? [...hex.slice(1)].map((value) => value.repeat(2)).join("") : hex.slice(1);
  const [red, green, blue] = full.match(/../g)!.map((value) => Number.parseInt(value, 16) / 255)
    .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrast(left: string, right: string): number {
  const values = [relativeLuminance(left), relativeLuminance(right)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

afterEach(() => {
  vi.restoreAllMocks();
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.themePreference;
  document.documentElement.style.removeProperty("color-scheme");
});

describe("presentation preferences", () => {
  it("selects explicit themes and resolves the system preference", () => {
    expect(resolvedTheme("light", true)).toBe("light");
    expect(resolvedTheme("dark", false)).toBe("dark");
    expect(resolvedTheme("system", true)).toBe("dark");
  });

  it("reacts to system color-scheme changes and removes its listener", () => {
    let listener: (() => void) | undefined;
    const media = {
      matches: false,
      addEventListener: vi.fn((_event: string, callback: () => void) => { listener = callback; }),
      removeEventListener: vi.fn(),
    };
    vi.stubGlobal("matchMedia", vi.fn(() => media));

    const dispose = installTheme("system");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.style.colorScheme).toBe("light");
    media.matches = true;
    listener?.();
    expect(document.documentElement.dataset.theme).toBe("dark");
    dispose();
    expect(media.removeEventListener).toHaveBeenCalledWith("change", expect.any(Function));
  });

  it("assigns stable, bounded accents independently of other identity fields", () => {
    const accent = identityAccentIndex("deploy-bot");
    expect(identityAccentIndex("deploy-bot")).toBe(accent);
    expect(identityAccentIndex("another-sender")).not.toBe(accent);
    expect(accent).toBeGreaterThanOrEqual(0);
    expect(accent).toBeLessThan(IDENTITY_ACCENT_COUNT);
  });

  it("keeps representative normal text, muted text, actions, and errors at AA contrast", () => {
    const pairs = [
      [":root", "text", "page"], [":root", "text-muted", "surface-muted"],
      [":root", "primary", "surface"], [":root", "danger", "danger-surface"],
      [":root[data-theme=\"dark\"]", "text", "page"],
      [":root[data-theme=\"dark\"]", "text-muted", "surface-muted"],
      [":root[data-theme=\"dark\"]", "primary", "surface"],
      [":root[data-theme=\"dark\"]", "danger", "danger-surface"],
    ];
    for (const [selector, foreground, background] of pairs) {
      expect(contrast(cssToken(selector, foreground), cssToken(selector, background))).toBeGreaterThanOrEqual(4.5);
    }
    for (let index = 0; index < IDENTITY_ACCENT_COUNT; index += 1) {
      expect(contrast(cssToken(":root", `identity-${index}`), cssToken(":root", "surface-muted"))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(cssToken(":root[data-theme=\"dark\"]", `identity-${index}`), cssToken(":root[data-theme=\"dark\"]", "surface"))).toBeGreaterThanOrEqual(4.5);
    }
    expect(styles).toContain(":focus-visible");
    expect(styles).toMatch(/button:disabled[^}]+opacity:/);
  });
});
