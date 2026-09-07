import { describe, expect, it } from 'vitest';
import {
  browserLocaleStrings,
  isUntranslatedMarker,
  nannosLocales,
  resolveLocaleStrings,
} from './locales';
import { nannosStringKeys } from './keys';

describe('locale resolution', () => {
  it('matches on the primary subtag', () => {
    expect(resolveLocaleStrings('de-CH')).toBe(nannosLocales.de);
    expect(resolveLocaleStrings('fr_CH')).toBe(nannosLocales.fr);
    expect(resolveLocaleStrings('IT')).toBe(nannosLocales.it);
  });

  it('takes the first tag it knows from a preference list', () => {
    expect(resolveLocaleStrings(['rm-CH', 'it-CH', 'de-CH'])).toBe(nannosLocales.it);
  });

  it('falls back to English for unknown or junk input', () => {
    expect(resolveLocaleStrings('rm-CH')).toBe(nannosLocales.en);
    expect(resolveLocaleStrings([])).toBe(nannosLocales.en);
    expect(resolveLocaleStrings(undefined)).toBe(nannosLocales.en);
    expect(resolveLocaleStrings('   ')).toBe(nannosLocales.en);
  });

  it('reads the browser preferences without throwing outside one', () => {
    expect(nannosStringKeys.every((k) => typeof browserLocaleStrings()[k] === 'string')).toBe(true);
  });

  it('every built-in table is complete', () => {
    for (const [name, table] of Object.entries(nannosLocales)) {
      const missing = nannosStringKeys.filter((k) => !table[k]);
      expect(missing, `${name} is missing keys`).toEqual([]);
    }
  });
});

describe('untranslated-marker overrides', () => {
  it('flags i18next missing-key echoes, prefixed or bare', () => {
    expect(isUntranslatedMarker('auth.title', 'nannos.sdk.auth.title')).toBe(true);
    expect(isUntranslatedMarker('auth.title', 'auth.title')).toBe(true);
  });

  it('keeps real translations', () => {
    expect(isUntranslatedMarker('auth.title', 'Freigabe erforderlich')).toBe(false);
    expect(isUntranslatedMarker('composer.send', 'Senden')).toBe(false);
  });
});
