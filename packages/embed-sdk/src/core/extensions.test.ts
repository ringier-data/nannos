import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ACTIVITY_LOG_KINDS, SUPPORTED_EXTENSIONS, X_A2A_EXTENSIONS_HEADER } from './extensions';
import { HITL_DECISION_TYPES } from '../transport/approval-codec';

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

  it('ACTIVITY_LOG_KINDS matches the repo-root registry', () => {
    const registry = JSON.parse(readFileSync(registryPath, 'utf8')).activityLogKinds as string[];
    expect([...ACTIVITY_LOG_KINDS].sort()).toEqual([...registry].sort());
  });

  it('HITL_DECISION_TYPES matches the repo-root registry', () => {
    // The server treats a type it does not know as a rejection, so drift here
    // silently blocks the very call the decision was meant to allow.
    const registry = JSON.parse(readFileSync(registryPath, 'utf8')).hitlDecisionTypes as string[];
    expect([...HITL_DECISION_TYPES].sort()).toEqual([...registry].sort());
  });
});
