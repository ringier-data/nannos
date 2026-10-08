import { defineConfig } from 'vitest/config';

// Unit tests for the node-side build tooling only (vite-plugins/). Deliberately NOT
// derived from vite.config.ts: the app's React/Tailwind plugins and the SDK source
// aliases are irrelevant here, and the app itself has no unit tests.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['vite-plugins/**/*.test.ts'],
  },
});
