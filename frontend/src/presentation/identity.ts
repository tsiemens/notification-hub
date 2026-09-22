export const IDENTITY_ACCENT_COUNT = 12;

/** FNV-1a gives an identity a stable, platform-independent palette slot. */
export function identityAccentIndex(identity: string): number {
  let hash = 0x811c9dc5;
  for (const character of identity) {
    hash ^= character.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash % IDENTITY_ACCENT_COUNT;
}
