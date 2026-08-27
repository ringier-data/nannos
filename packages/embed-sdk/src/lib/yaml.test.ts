import { describe, expect, it } from 'vitest';
import { toYaml, toYamlTokens } from './yaml';

describe('toYaml', () => {
  it('renders nested objects with plain keys and quoted risky scalars', () => {
    const value = {
      kind: 'status-update',
      status: {
        state: 'TASK_STATE_INPUT_REQUIRED',
        timestamp: '2026-08-27T12:18:13.987957Z',
      },
      validation_errors: [],
    };
    expect(toYaml(value)).toBe(
      [
        'kind: status-update',
        'status:',
        '  state: TASK_STATE_INPUT_REQUIRED',
        // ':' inside → quoted, so the timestamp can never misparse as a mapping.
        '  timestamp: "2026-08-27T12:18:13.987957Z"',
        'validation_errors: []',
      ].join('\n'),
    );
  });

  it('folds objects in sequences onto the dash', () => {
    expect(toYaml({ parts: [{ text: 'Hello', kind: 'text' }, { data: { x: 1 } }] })).toBe(
      ['parts:', '  - text: Hello', '    kind: text', '  - data:', '      x: 1'].join('\n'),
    );
  });

  it('renders multiline text as a literal block', () => {
    expect(toYaml({ text: 'line one\nline two' })).toBe(
      ['text: |-', '  line one', '  line two'].join('\n'),
    );
  });

  it('quotes strings that would misparse as YAML types', () => {
    expect(toYaml({ a: 'null', b: '123', c: 'yes', d: '- dash' })).toBe(
      ['a: "null"', 'b: "123"', 'c: "yes"', 'd: "- dash"'].join('\n'),
    );
  });

  it('keeps numbers, booleans and null bare, and drops undefined like JSON', () => {
    expect(toYaml({ n: 3, b: false, z: null, gone: undefined })).toBe(
      ['n: 3', 'b: false', 'z: null'].join('\n'),
    );
  });

  it('handles empty collections and scalar roots', () => {
    expect(toYaml({})).toBe('{}');
    expect(toYaml([])).toBe('[]');
    expect(toYaml('hi')).toBe('hi');
    expect(toYaml(undefined)).toBe('—');
  });

  it('emits typed tokens the highlighter can color without re-parsing', () => {
    const lines = toYamlTokens({ kind: 'status-update', n: 3, ok: true, note: 'a: b' });
    expect(lines.map((line) => line.map((token) => [token.t, token.s]))).toEqual([
      [['key', 'kind'], ['punct', ':'], ['punct', ' '], ['str', 'status-update']],
      [['key', 'n'], ['punct', ':'], ['punct', ' '], ['num', '3']],
      [['key', 'ok'], ['punct', ':'], ['punct', ' '], ['bool', 'true']],
      // ':' inside → quoted, but still ONE string token: never mistaken for a key.
      [['key', 'note'], ['punct', ':'], ['punct', ' '], ['str', '"a: b"']],
    ]);
  });

  it('marks a cycle instead of throwing', () => {
    const value: Record<string, unknown> = { name: 'loop' };
    value.self = value;
    expect(toYaml(value)).toBe(['name: loop', 'self: "[circular]"'].join('\n'));
  });
});
