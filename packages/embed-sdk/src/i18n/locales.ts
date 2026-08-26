/**
 * The built-in locale tables and the browser-locale resolver.
 *
 * The SDK ships four complete tables (en, de, fr, it — the house languages).
 * A host that speaks another language still overrides per key via
 * `<NannosProvider strings={…}>`; those overrides merge OVER whichever table
 * the locale picks, so a host only translates what it cares about.
 *
 * Matching is on the primary subtag only ('de-CH' → 'de'), because none of the
 * tables is region-specific. Unknown languages fall back to English rather
 * than to a partially translated table.
 */
import type { NannosStrings } from './keys';
import { en } from './en';
import { de } from './de';
import { fr } from './fr';
import { it } from './it';

export const nannosLocales = { en, de, fr, it } as const;

export type NannosLocale = keyof typeof nannosLocales;

/** Primary subtag, lowercased ('de-CH' → 'de'); '' for junk input. */
function primarySubtag(tag: string): string {
  return tag.trim().toLowerCase().split(/[-_]/)[0] ?? '';
}

/**
 * First tag with a built-in table wins; English when none match.
 * Accepts one tag or a preference list (`navigator.languages` order).
 */
export function resolveLocaleStrings(
  tags: string | readonly string[] | undefined | null,
): NannosStrings {
  const list = typeof tags === 'string' ? [tags] : (tags ?? []);
  for (const tag of list) {
    if (typeof tag !== 'string') continue;
    const table = nannosLocales[primarySubtag(tag) as NannosLocale];
    if (table) return table;
  }
  return en;
}

/**
 * The table for the browser's language preferences. Safe outside a browser
 * (SSR, unit tests without a DOM): falls back to English.
 */
export function browserLocaleStrings(): NannosStrings {
  if (typeof navigator === 'undefined') return en;
  const langs = navigator.languages;
  return resolveLocaleStrings(
    Array.isArray(langs) && langs.length > 0 ? langs : navigator.language,
  );
}

/**
 * True when a host override is an UNTRANSLATED-KEY MARKER, not a translation.
 * Hosts build their override maps mechanically (`t('nannos.sdk.' + key)` for
 * every `nannosStringKeys` entry — the cockpit does exactly this), and i18next
 * returns the lookup key itself when the catalog has no entry. Merging that in
 * would render literal "nannos.sdk.auth.title" chrome, so such values are
 * dropped and the key falls back to the built-in locale table.
 */
export function isUntranslatedMarker(key: string, value: string): boolean {
  return value === key || value.endsWith(`.${key}`);
}
