import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { clientActionDirective } from './schemas';

// Pin the widget's directive-kind vocabulary to the repo-root registry. The
// Python tool schema (agent-common client_action_tool.py) pins the same list:
// a kind the agent can emit but the widget refuses is a silent no-op reported
// to the user as done.
const registryPath = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../a2a-extensions.json');

describe('client-action kind vocabulary conformance', () => {
  it('the zod union matches the repo-root registry', () => {
    const registry = JSON.parse(readFileSync(registryPath, 'utf8')).clientActionKinds as string[];
    const zodKinds = clientActionDirective.options.map(
      (option) => (option.shape.kind as { value: string }).value,
    );
    expect([...zodKinds].sort()).toEqual([...registry].sort());
  });
});
