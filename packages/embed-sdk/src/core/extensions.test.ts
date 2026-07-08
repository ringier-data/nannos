import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { SUPPORTED_EXTENSIONS, X_A2A_EXTENSIONS_HEADER } from './extensions';

// Pin the SDK's extension list to the repo-root a2a-extensions.json registry.
// Orchestrator and console-backend carry their own copies pinned the same way,
// so adding an extension anywhere fails tests until every copy agrees.
const registryPath = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../a2a-extensions.json');

describe('A2A extension registry conformance', () => {
  it('SUPPORTED_EXTENSIONS matches the repo-root registry', () => {
    const registry = JSON.parse(readFileSync(registryPath, 'utf8')).extensions as string[];
    expect([...SUPPORTED_EXTENSIONS].sort()).toEqual([...registry].sort());
  });

  it('the negotiation header carries every extension', () => {
    for (const urn of SUPPORTED_EXTENSIONS) {
      expect(X_A2A_EXTENSIONS_HEADER).toContain(urn);
    }
  });
});
