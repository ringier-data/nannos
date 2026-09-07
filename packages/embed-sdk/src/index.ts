// Public package entry — the LIGHT surface: the framework-free core plus the
// React bindings (provider, hooks, host adapter). No chat UI, no `ai`, no
// Radix here; the heavy chat panel lives behind '@nannos/embed-sdk/panel'
// (its own entry — the host's lazy-chunk boundary), and the framework-free
// chat engine behind '@nannos/embed-sdk/transport'.
export * from './core';
export * from './react';
