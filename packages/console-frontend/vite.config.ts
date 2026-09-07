import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';
import fs from 'fs';

const sdkSrc = path.resolve(__dirname, '../embed-sdk/src');
// Only true while the SDK is a workspace sibling. If it is ever consumed as a
// published tarball (files: ["dist"] — no src/), fall through to its package
// `exports` instead of aliasing to a path that doesn't exist. NOTE: the
// `@source '../../embed-sdk/src'` line in src/index.css has the same dependency
// and does NOT degrade — it would need repointing at the installed dist.
const sdkSourceLinked = fs.existsSync(path.join(sdkSrc, 'index.ts'));

// In dev the linked @nannos/embed-sdk is consumed as SOURCE, not through its
// `dist` (which is what package.json `exports` points at, and what `vite build`
// keeps using). Reading dist in dev meant watching a tree that
// `vite build --watch` rewrites non-atomically with renumbered rollup chunks —
// producing half-read modules and stale importers ("does not provide an export
// named ...") that had to be papered over with no-cache middleware, write-settle
// delays, optimizeDeps opt-outs and hardcoded react paths. Compiling the source
// removes the cause: real HMR, and one react/zod instance by construction.
//
// Order matters: aliases match on a path-segment boundary, so the subpath
// entries must precede the bare one or `/panel` would resolve under index.ts.
// The SDK's own compiled sheet is NOT needed here — the console styles SDK
// components in its own light DOM (see the @source lines in src/index.css).
const embedSdkSourceAliases = [
  { find: '@nannos/embed-sdk/core', replacement: path.join(sdkSrc, 'core/index.ts') },
  { find: '@nannos/embed-sdk/react', replacement: path.join(sdkSrc, 'react/index.ts') },
  { find: '@nannos/embed-sdk/transport', replacement: path.join(sdkSrc, 'transport/index.ts') },
  { find: '@nannos/embed-sdk/panel', replacement: path.join(sdkSrc, 'panel/index.ts') },
  { find: '@nannos/embed-sdk', replacement: path.join(sdkSrc, 'index.ts') },
];

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:5001',
        changeOrigin: true,
        ws: true, // Explicitly enable WebSocket proxying
      },
      '/mcp': {
        target: 'http://localhost:5001',
        changeOrigin: true,
      },
    },
  },
  resolve: {
    alias: [
      ...(command === 'serve' && sdkSourceLinked ? embedSdkSourceAliases : []),
      { find: '@', replacement: path.resolve(__dirname, './src') },
    ],
    dedupe: ['react', 'react-dom'],
  },
}));
