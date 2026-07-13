import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'node:path';

// Library mode → ESM (npm / React hosts) + IIFE single-file (self-hosted by a
// host's own CDN; never a Nannos-origin auto-update — the SDK is in the
// on-behalf-of token path, see ADR-0002 / CONTEXT.md).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  build: {
    lib: {
      entry: {
        index: resolve(__dirname, 'src/index.ts'),
        'core/index': resolve(__dirname, 'src/core/index.ts'),
        'react/index': resolve(__dirname, 'src/react/index.ts'),
      },
      formats: ['es'],
    },
    rollupOptions: {
      // React is a peer dep for the ESM build; the IIFE "any-site" bundle would
      // instead bundle React in (separate config, omitted at spike stage).
      // Externalize the ENTIRE react/react-dom trees — critically including
      // `react/jsx-runtime` (what the JSX transform emits). If it were bundled,
      // the SDK would ship its OWN (dev-dep, React 19) jsx-runtime and run it
      // against a host on React 18 → "Cannot read properties of undefined
      // (reading 'recentlyCreatedOwnerStacks')". Host must supply React.
      //
      // zod is likewise externalized (peerDependency): bundling it would give the
      // SDK its own copy, so `zodFormRegistration`/`zodToFieldSpecs` would run
      // `z.toJSONSchema` on a schema the HOST built with ITS zod — a cross-copy call
      // that isn't guaranteed to work. External → the host's single zod instance is
      // used everywhere.
      external: (id) =>
        id === 'react' ||
        id === 'react-dom' ||
        id.startsWith('react/') ||
        id.startsWith('react-dom/') ||
        id === 'zod' ||
        id.startsWith('zod/'),
    },
    cssCodeSplit: false,
  },
});
