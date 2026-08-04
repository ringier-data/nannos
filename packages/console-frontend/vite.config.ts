import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';

// https://vite.dev/config/
export default defineConfig({
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
    alias: {
      '@': path.resolve(__dirname, './src'),
      // @nannos/embed-sdk is a linked workspace package that also depends on
      // react/react-dom. Those are hoisted to the repo-root node_modules (no
      // console-frontend/node_modules/react copy exists), so Vite's dep OPTIMIZER
      // — triggered to re-scan whenever embed-sdk's dist is rebuilt — fails to
      // locate react.js and 504s every dep (blank app). `dedupe` alone doesn't fix
      // this (it governs module resolution, not the optimizer's file lookup), so
      // pin react/react-dom to their real hoisted paths. Prefix aliases also cover
      // react/jsx-runtime, react-dom/client, etc.
      react: path.resolve(__dirname, '../../node_modules/react'),
      'react-dom': path.resolve(__dirname, '../../node_modules/react-dom'),
    },
    dedupe: ['react', 'react-dom'],
  },
  optimizeDeps: {
    // Don't pre-bundle the linked workspace SDK. Scanning its large ESM dist during
    // dep-optimization is what drags react into the optimizer from the SDK's context
    // and wedges the whole pass (react.js never gets written → every dep 504s → blank
    // app), and it re-triggers on every SDK rebuild. It's already ESM, so Vite serves
    // it directly; react/react-dom still get optimized from the app's own imports.
    exclude: ['@nannos/embed-sdk'],
  },
});
