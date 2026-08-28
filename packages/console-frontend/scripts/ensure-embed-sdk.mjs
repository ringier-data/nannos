// `prebuild` guard: make sure @nannos/embed-sdk's dist is present and no older
// than its source before the console is bundled.
//
// Why this is needed: the dev loop deliberately does NOT keep dist/*.js current.
// The console dev server compiles the SDK's source (serve-only aliases in
// vite.config.ts) and start-local runs the SDK's `dev:link` — tsc + tailwind
// watch only, no `vite build --watch`, whose non-atomic dist rewrites used to
// wedge the dev server mid-rebuild. So after an SDK source edit in a dev
// session dist JS is stale, while `vite build` still resolves the SDK through
// package exports → dist. Without this guard a local prod build would silently
// bundle stale SDK code.
//
// Two no-ops by design:
//   - SDK not a workspace sibling (installed from a registry: files: ["dist"],
//     no src/) → nothing to build, the installed dist is authoritative.
//   - dist already newer than every source file (CI/Docker build the SDK in
//     their own earlier step) → don't pay for a second build.
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const sdk = join(dirname(fileURLToPath(import.meta.url)), '../../embed-sdk');
const src = join(sdk, 'src');

if (!existsSync(src)) {
  console.log('[ensure-embed-sdk] no sibling source — using the installed @nannos/embed-sdk dist.');
  process.exit(0);
}

const newestMtime = (dir) => {
  let newest = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    newest = Math.max(newest, entry.isDirectory() ? newestMtime(full) : statSync(full).mtimeMs);
  }
  return newest;
};

// Both sentinels matter: index.js is what `vite build` bundles, index.d.ts is
// what `tsc -b` reads, and `dev:link` refreshes only the latter. Whichever is
// older decides.
const sentinels = ['dist/index.js', 'dist/index.d.ts'].map((f) => join(sdk, f));
const builtAt = sentinels.every(existsSync)
  ? Math.min(...sentinels.map((f) => statSync(f).mtimeMs))
  : 0;

if (builtAt > newestMtime(src)) {
  console.log('[ensure-embed-sdk] dist is up to date.');
  process.exit(0);
}

console.log('[ensure-embed-sdk] building @nannos/embed-sdk (dist missing or stale)...');
execFileSync('npm', ['run', 'build'], { cwd: sdk, stdio: 'inherit' });
