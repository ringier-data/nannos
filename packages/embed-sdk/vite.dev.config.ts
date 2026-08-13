import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'node:path';

// Dev harness only (`npm run dev` → index.html → dev/main.tsx). Kept separate from
// vite.config.ts because that one is library mode: it has no HTML entry and
// externalizes react, so it can't serve a page. Nothing here ships.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5180,
  },
});
