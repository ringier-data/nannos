#!/usr/bin/env node
/**
 * Vendor AI Elements (https://elements.ai-sdk.dev) into
 * src/components/ai-elements, applying this SDK's codemod:
 *
 *  - `@/registry/default/ui/X` and `@/registry/new-york-v4/ui/X` → `../ui/X`
 *    (our shadcn primitives — already portal-container-aware for Shadow DOM)
 *  - `@/registry/default/ai-elements/X` → `./X` (cross-component refs)
 *  - `@/lib/utils` → `../../lib/utils`
 *  - `import { X } from "radix-ui"` umbrella → scoped `@radix-ui/react-*`
 *    is NOT auto-rewritten — flagged instead (rewrite by hand, see ui/button-group).
 *
 * Usage: node scripts/vendor-ai-elements.mjs [component ...]
 * Without args, vendors the pinned COMPONENTS list. Prints every component's
 * npm deps and registry deps so missing primitives/packages are visible.
 * Re-running overwrites — local patches beyond the codemod belong in separate
 * files or must be re-applied (keep them minimal and commented).
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REGISTRY = 'https://elements.ai-sdk.dev/api/registry';

/** The pinned set: the chatbot surface (full adoption) + code-block (tool dep). */
const COMPONENTS = [
  'conversation',
  'message',
  'prompt-input',
  'reasoning',
  'tool',
  'confirmation',
  'task',
  'plan',
  'chain-of-thought',
  'sources',
  'suggestion',
  'shimmer',
  'attachments',
  'model-selector',
  'queue',
  'code-block',
];

const outDir = join(dirname(fileURLToPath(import.meta.url)), '../src/components/ai-elements');

function codemod(source) {
  return source
    .replaceAll(/@\/registry\/(?:default|new-york-v4)\/ui\//g, '../ui/')
    .replaceAll(/@\/registry\/(?:default|new-york-v4)\/ai-elements\//g, './')
    .replaceAll(/(['"])@\/components\/ui\//g, '$1../ui/')
    .replaceAll(/(['"])@\/lib\/utils(['"])/g, '$1../../lib/utils$2')
    .replaceAll(/(['"])@\/lib\/portal-container(['"])/g, '$1../../lib/portal-container$2');
}

const targets = process.argv.slice(2).length ? process.argv.slice(2) : COMPONENTS;
mkdirSync(outDir, { recursive: true });

const written = [];
for (const name of targets) {
  const res = await fetch(`${REGISTRY}/${name}.json`);
  if (!res.ok) {
    console.error(`✗ ${name}: HTTP ${res.status}`);
    process.exitCode = 1;
    continue;
  }
  const def = await res.json();
  if (!def.files?.length) {
    console.error(`✗ ${name}: registry entry has no files`);
    process.exitCode = 1;
    continue;
  }
  for (const file of def.files) {
    const base = file.path.split('/').pop();
    const content = codemod(file.content);
    writeFileSync(join(outDir, base), content);
    written.push(base);
    if (/from ["']radix-ui["']/.test(content)) {
      console.warn(`  ⚠ ${base}: imports the "radix-ui" umbrella — rewrite to the scoped package by hand`);
    }
  }
  console.log(
    `✓ ${name}  deps=[${(def.dependencies ?? []).join(', ')}]  registryDeps=[${(def.registryDependencies ?? []).join(', ')}]`,
  );
}

// Regenerate the barrel.
const modules = [...new Set(written)].filter((f) => f.endsWith('.tsx')).sort();
writeFileSync(
  join(outDir, 'index.ts'),
  '// Vendored AI Elements (elements.ai-sdk.dev shadcn registry), codemodded for\n' +
    '// this SDK: relative imports, portal-container-aware primitives underneath.\n' +
    '// Re-vendor via scripts/vendor-ai-elements.mjs; keep local patches minimal\n' +
    '// and commented.\n' +
    modules.map((f) => `export * from './${f.replace(/\.tsx$/, '')}';`).join('\n') +
    '\n',
);
console.log(`\nbarrel: ${modules.length} modules`);
