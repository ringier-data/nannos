// '@nannos/embed-sdk/react' — the React layer: the host-authority provider
// (open/pin/width/seeding/gesture-login), form registration, the host adapter,
// PKCE surface, and the i18n seam. Deliberately LIGHT: no `ai`, no Radix, no
// chat UI — those live behind '@nannos/embed-sdk/panel'.
export {
  NannosProvider,
  useAssistant,
  useNannosStatus,
  clampPanelWidth,
  NANNOS_PANEL_WIDTH_VAR,
  type AssistantValue,
  type NannosProviderProps,
  type OpenOptions,
  type PageContextLayerHandle,
  type SeededPrompt,
} from './provider';
export { useNannosPageContext } from './use-page-context';
export { useNannosPageReader } from './use-page-reader';
export type { NannosPageContext, NannosPageEntity, NannosPageReader } from '../core';
export * from './adapter';
export { pkce, NannosAuthCallback } from './auth-callback';
export { useNannosZodForm, type FormLike, type UseNannosZodFormOptions } from './use-nannos-form';
export * from './object-registry';
export { createNannosForm, type UseNannosFormOptions } from './create-nannos-form';
export { useObjectStateAdapter } from './state-adapter';
export { NannosStringsProvider, useStrings, format } from './i18n';
// Theming lives here (not only in /panel) so hosts can build their override
// sheet in the EAGER bundle while the panel chunk stays lazy.
export { themeSheet, type NannosTheme } from '../styles/theme-sheet';
export { nannosStringKeys, type NannosStrings } from '../i18n/keys';
export { en as nannosDefaultStrings } from '../i18n/en';
export {
  nannosLocales,
  resolveLocaleStrings,
  browserLocaleStrings,
  type NannosLocale,
} from '../i18n/locales';
