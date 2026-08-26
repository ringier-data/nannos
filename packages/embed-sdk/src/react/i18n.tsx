/**
 * The i18n seam: a context carrying the merged string table and a
 * `{placeholder}` micro-formatter. Every panel component reads through
 * `useStrings()` — no hardcoded chrome strings.
 *
 * Three layers, in order of precedence:
 *   1. host overrides (`strings`) — per key, so a partial map is fine;
 *   2. the built-in table for the requested/browser locale (en, de, fr, it);
 *   3. English.
 * The locale defaults to the browser's preference list, so an embed renders in
 * the end-user's language with no host wiring at all.
 */
import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { en } from '../i18n/en';
import { browserLocaleStrings, isUntranslatedMarker, resolveLocaleStrings } from '../i18n/locales';
import type { NannosStrings } from '../i18n/keys';

const StringsContext = createContext<NannosStrings>(en);

export function NannosStringsProvider({
  locale,
  strings,
  children,
}: {
  /** Force a locale (e.g. the host's own language switch). Default: the browser's. */
  locale?: string | readonly string[];
  strings?: Partial<NannosStrings>;
  children: ReactNode;
}): ReactNode {
  // Reactive: a host switching language passes a new locale or object identity.
  const merged = useMemo<NannosStrings>(() => {
    const base = locale ? resolveLocaleStrings(locale) : browserLocaleStrings();
    if (!strings) return base;
    // Drop undefined/empty overrides so a half-built host map falls back per
    // key — and untranslated-key markers ("nannos.sdk.auth.title"), which
    // i18next-style hosts emit for keys their catalogs don't know yet.
    const cleaned = Object.fromEntries(
      Object.entries(strings).filter(
        ([k, v]) => typeof v === 'string' && v !== '' && !isUntranslatedMarker(k, v),
      ),
    );
    return { ...base, ...cleaned };
  }, [locale, strings]);
  return <StringsContext.Provider value={merged}>{children}</StringsContext.Provider>;
}

/** The merged string table (English defaults outside any provider). */
export function useStrings(): NannosStrings {
  return useContext(StringsContext);
}

/** `{placeholder}` interpolation. Unknown placeholders are left as-is. */
export function format(template: string, params?: Record<string, string | number>): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in params ? String(params[key]) : match,
  );
}
