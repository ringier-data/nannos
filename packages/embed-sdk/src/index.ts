// Public package entry (full bundle: core + React UI kit).
// Headless-only consumers should import from "@nannos/embed-sdk/core".

import { ChatWidget } from './ui/ChatWidget';

export * from './core';
export { ChatWidget } from './ui/ChatWidget';
export {
  HostAdapterProvider,
  useHostAdapter,
  useNannosCore,
  useNannosCoreOptional,
  useNannosCoreConfig,
  resolveHostAdapter,
  backendFetch,
  type NannosHostAdapter,
  type ResolvedHostAdapter,
  type FeedbackRating,
  type FeedbackItem,
  type UploadedFileInfo,
  type UserChatSettings,
} from './ui/adapter';
// The extracted chat surface (console-frontend consumes these — one UI).
export * from './ui/chat';
export { mount, type MountOptions, type MountedWidget } from './element';

// Drop-in floating widget. The React bindings (NannosProvider / useNannos /
// useNannosZodForm) live in "@nannos/embed-sdk/react" — import them from there so
// there's a single provider context; NannosWidget shares it via the same chunk.
export { NannosWidget, type NannosWidgetProps } from './ui/NannosWidget';

// Optional: register a custom element for non-React hosts.
// (r2wc is loaded lazily so importing this module doesn't force a definition.)
export async function defineElement(tag = 'nannos-assistant') {
  const { default: r2wc } = await import('@r2wc/react-to-web-component');
  // `core` is passed as a property by the host after construction.
  const El = r2wc(ChatWidget as never, { shadow: 'open', props: { core: 'function' } as never });
  if (!customElements.get(tag)) customElements.define(tag, El as never);
}
