import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'node:path';

// Two modes:
//  - `vite` (serve)  → the dev/ harness app: the live verification page for the
//    transport (Phase-0 S1–S5 gates) and the SDK dev playground. Same-origin
//    API/socket traffic is proxied to a local console-backend; port 3000 on
//    purpose — it is in the backend's EMBED_ALLOWED_ORIGINS, so the harness can
//    also point at stg/prod with a pasted token.
//  - `vite build`    → library mode, ESM (npm / React hosts). React and zod are
//    peer deps and must stay external (see the comments on `external`).
export default defineConfig(({ command, mode }) => {
  const shared = {
    plugins: [react(), tailwindcss()],
  };

  // vitest also loads this config with command === 'serve' — it must NOT get
  // the dev-harness root, or it roots at dev/ and finds zero test files.
  if (command === 'serve' && mode !== 'test') {
    return {
      ...shared,
      root: resolve(__dirname, 'dev'),
      server: {
        port: 3000,
        proxy: {
          '/api': {
            target: 'http://localhost:5001',
            changeOrigin: true,
            ws: true, // socket.io websocket upgrade
          },
        },
      },
    };
  }

  return {
    ...shared,
    build: {
      // The `build` script rm -rf's dist itself. Vite must NOT empty it:
      // `build:watch` runs vite and `tsc --watch` side by side, and wiping
      // dist at startup would delete the .d.ts files tsc maintains there.
      emptyOutDir: false,
      lib: {
        entry: {
          index: resolve(__dirname, 'src/index.ts'),
          'core/index': resolve(__dirname, 'src/core/index.ts'),
          'react/index': resolve(__dirname, 'src/react/index.ts'),
          'transport/index': resolve(__dirname, 'src/transport/index.ts'),
          'panel/index': resolve(__dirname, 'src/panel/index.ts'),
        },
        formats: ['es'] as const,
      },
      rollupOptions: {
        // React is a peer dep for the ESM build. Externalize the ENTIRE
        // react/react-dom trees — critically including `react/jsx-runtime`
        // (what the JSX transform emits). If it were bundled, the SDK would
        // ship its OWN (dev-dep, React 19) jsx-runtime and run it against a
        // host on React 18 → "Cannot read properties of undefined (reading
        // 'recentlyCreatedOwnerStacks')". Host must supply React.
        //
        // zod is likewise externalized (peerDependency): bundling it would give
        // the SDK its own copy, so `zodFormRegistration`/`zodToFieldSpecs`
        // would run `z.toJSONSchema` on a schema the HOST built with ITS zod —
        // a cross-copy call that isn't guaranteed to work. External → the
        // host's single zod instance is used everywhere.
        //
        // `ai`/`@ai-sdk/react` are exact-pinned regular deps and BUNDLE (their
        // react imports still resolve to the host's React through the
        // externals above) — hosts never install or version-manage them.
        external: (id: string) =>
          id === 'react' ||
          id === 'react-dom' ||
          id.startsWith('react/') ||
          id.startsWith('react-dom/') ||
          id === 'zod' ||
          id.startsWith('zod/'),
        // One dist file per src module (shared modules exist exactly once →
        // the provider context is a singleton across all entries; hosts
        // tree-shake and code-split at module granularity).
        output: {
          preserveModules: true,
          preserveModulesRoot: resolve(__dirname, 'src'),
          entryFileNames: '[name].js',
        },
      },
      cssCodeSplit: false,
    },
  };
});
