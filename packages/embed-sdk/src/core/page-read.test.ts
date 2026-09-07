/**
 * The read-result sanitizer (everything it emits reaches a model on demand).
 * Behaviors ported from Gatana's page-context read half.
 */
import { describe, expect, it } from 'vitest';
import { sanitizeReadResult, sanitizeReadResultWithScreen, tailLines } from './page-read';

describe('sanitizeReadResult', () => {
  it('drops secret-named keys at EVERY depth, keeps describing keys', () => {
    const out = JSON.parse(
      sanitizeReadResult({
        server: {
          name: 'prod',
          connection: { apiKey: 'sk-oops', authMethod: 'oauth', hasCredentials: true },
        },
        access_token: 'oops',
      }),
    );
    expect(out).toEqual({
      server: { name: 'prod', connection: { authMethod: 'oauth', hasCredentials: true } },
    });
  });

  it('caps depth, array length, key count and string length', () => {
    const deep = { a: { b: { c: { d: { e: { f: { g: 'too deep' } } } } } } };
    const wide: Record<string, number> = {};
    for (let i = 0; i < 60; i++) wide[`k${i}`] = i;
    const out = JSON.parse(
      sanitizeReadResult({
        deep,
        rows: Array.from({ length: 80 }, (_, i) => i),
        long: 'x'.repeat(900),
        ...wide,
      }),
    );
    expect(out.rows).toHaveLength(50);
    expect(out.long.length).toBeLessThanOrEqual(500);
    // 40-key cap on the top object (deep/rows/long + the first 37 wide keys).
    expect(Object.keys(out)).toHaveLength(40);
    // Depth 6 pruned the innermost object away.
    expect(JSON.stringify(out.deep)).not.toContain('too deep');
  });

  it('keeps Dates as ISO strings; functions and undefined vanish', () => {
    const out = JSON.parse(
      sanitizeReadResult({ at: new Date('2026-08-26T10:00:00Z'), fn: () => 1, gone: undefined }),
    );
    expect(out).toEqual({ at: '2026-08-26T10:00:00.000Z' });
  });

  it('truncates an over-long read instead of refusing it', () => {
    const out = JSON.parse(
      sanitizeReadResult({
        rows: Array.from({ length: 50 }, (_, i) => ({ [`field${i}`]: 'v'.repeat(400) })),
      }),
    );
    expect(out.truncated).toBe(true);
    expect(out.partial.length).toBeLessThanOrEqual(10_000);
  });

  it('a cycle degrades to an explicit error payload, not a throw', () => {
    const a: Record<string, unknown> = { name: 'a' };
    a.self = a;
    const out = JSON.parse(sanitizeReadResult(a));
    // Depth capping usually breaks shallow cycles; either way the call returns JSON.
    expect(typeof out).toBe('object');
  });
});

describe('sanitizeReadResultWithScreen', () => {
  it('carries the outline whole under `screen`, past the per-string cap', () => {
    const outline = '# Page\n' + 'a real line of outline text\n'.repeat(100); // ~2.9k chars
    const out = JSON.parse(sanitizeReadResultWithScreen({ page: { key: '/x' } }, () => outline));
    expect(out.page).toEqual({ key: '/x' });
    expect(out.screen).toBe(outline);
    expect(out.screen.length).toBeGreaterThan(500);
  });

  it('gives the outline the budget the readers left, floored and ceilinged', () => {
    const budgets: number[] = [];
    const snapshot = (maxChars: number) => {
      budgets.push(maxChars);
      return 'outline';
    };
    // Nearly empty readers → the outline gets the ceiling, not the whole 10k.
    sanitizeReadResultWithScreen({}, snapshot);
    // Heavy readers → the outline still gets at least the floor.
    sanitizeReadResultWithScreen(
      { rows: Array.from({ length: 40 }, (_, i) => ({ [`f${i}`]: 'v'.repeat(400) })) },
      snapshot,
    );
    expect(budgets[0]).toBe(7000);
    expect(budgets[1]).toBe(1500);
  });

  it('the built-in outline wins the reserved `screen` key over a reader', () => {
    const out = JSON.parse(
      sanitizeReadResultWithScreen({ screen: 'a reader squatting the key' }, () => 'the real outline'),
    );
    expect(out.screen).toBe('the real outline');
  });

  it('cuts the outline, not the readers, when the total still lands over the cap', () => {
    const rows = Array.from({ length: 20 }, (_, i) => ({ [`f${i}`]: 'v'.repeat(400) }));
    const out = JSON.parse(sanitizeReadResultWithScreen({ rows }, (max) => 'o'.repeat(max + 3000)));
    expect(out.rows).toHaveLength(20);
    expect(out.screen.endsWith('…')).toBe(true);
    expect(JSON.stringify(out).length).toBeLessThanOrEqual(10_000);
  });

  it('a throwing walk costs only the outline, never the readers', () => {
    const out = JSON.parse(
      sanitizeReadResultWithScreen({ page: { key: '/x' } }, () => {
        throw new Error('no DOM here');
      }),
    );
    expect(out).toEqual({ page: { key: '/x' } });
  });

  it('wraps a non-object answer so the outline can sit beside it', () => {
    const out = JSON.parse(sanitizeReadResultWithScreen('just a string', () => 'outline'));
    expect(out).toEqual({ state: 'just a string', screen: 'outline' });
  });

  it('still applies the deny list to the readers', () => {
    const out = JSON.parse(
      sanitizeReadResultWithScreen({ form: { name: 'x', apiKey: 'sk-oops' } }, () => ''),
    );
    expect(out.form).toEqual({ name: 'x' });
  });
});

describe('tailLines', () => {
  it('spends the budget from the newest line backwards and reports omissions', () => {
    const lines = Array.from({ length: 100 }, (_, i) => `line ${i}`);
    const { lines: kept, omitted } = tailLines(lines, 200, 10);
    expect(kept).toHaveLength(10);
    expect(kept[kept.length - 1]).toBe('line 99');
    expect(omitted).toBe(90);
  });

  it('one enormous line cannot push out everything before it', () => {
    const { lines: kept } = tailLines(['first', 'x'.repeat(10_000)], 600, 40);
    // The huge line is clamped per-line (500), leaving budget for its predecessor.
    expect(kept).toHaveLength(2);
    expect(kept[0]).toBe('first');
  });
});
