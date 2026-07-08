// Console shim over the Embed SDK chat surface. The chat UI lives in
// @nannos/embed-sdk (one UI — the embeddable widget and console share it);
// console contributes only its host adapter (see consoleAdapter.tsx).
export * from '@nannos/embed-sdk';
export { ConsoleHostAdapterProvider } from './consoleAdapter';
