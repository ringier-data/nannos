import { describe, expect, it } from 'vitest';
import { clientActionKind, clientActionSummaryKey, toolPartTitle } from './tool-title';

describe('toolPartTitle', () => {
  it('leaves every other tool alone', () => {
    expect(toolPartTitle('ls', { path: '/' })).toBe('ls');
    expect(toolPartTitle('search', undefined)).toBe('search');
  });

  it('names the kind on the round-trip shape (wrapped wire directive)', () => {
    expect(
      toolPartTitle('client_action', {
        directive: { kind: 'read_current_page' },
        _clientActionRequest: true,
      }),
    ).toBe('client_action · read_current_page');
  });

  it('names the kind on the approval shape (flat, snake_case tool args)', () => {
    // What the risk gate surfaces for an `apply`: the agent's raw arguments.
    expect(
      toolPartTitle('client_action', {
        kind: 'apply',
        target_type: 'Campaign',
        target_id: '42',
        values: { name: 'x' },
        _risk_metadata: { source: 'risk_score', score: 0.9 },
      }),
    ).toBe('client_action · apply');
  });

  it('prefers the wrapped directive when both shapes are present', () => {
    expect(
      toolPartTitle('client_action', { kind: 'navigate', directive: { kind: 'highlight' } }),
    ).toBe('client_action · highlight');
  });

  it('falls back to the bare name rather than titling with a gap or `undefined`', () => {
    for (const input of [undefined, null, {}, 'garbled', { directive: null }, { kind: '  ' }]) {
      expect(toolPartTitle('client_action', input)).toBe('client_action');
    }
  });
});

describe('clientActionKind', () => {
  it('reads through either shape and refuses a non-string kind', () => {
    expect(clientActionKind({ directive: { kind: 'apply' } })).toBe('apply');
    expect(clientActionKind({ kind: 'navigate' })).toBe('navigate');
    expect(clientActionKind({ kind: 7 })).toBeNull();
  });
});

describe('clientActionSummaryKey', () => {
  // The agent no longer pays a fast-LLM call to explain a closed enum, so the
  // card needs its own sentence — localized, and only for kinds we know.
  it('maps every known kind, in either arg shape', () => {
    expect(clientActionSummaryKey({ kind: 'apply' })).toBe('hitl.clientAction.apply');
    expect(clientActionSummaryKey({ directive: { kind: 'highlight' } })).toBe(
      'hitl.clientAction.highlight',
    );
    expect(clientActionSummaryKey({ kind: 'navigate' })).toBe('hitl.clientAction.navigate');
    expect(clientActionSummaryKey({ kind: 'read_current_page' })).toBe(
      'hitl.clientAction.readCurrentPage',
    );
  });

  it('says nothing about a kind it does not know', () => {
    // A confident sentence about the wrong action is worse than raw args.
    expect(clientActionSummaryKey({ kind: 'refresh' })).toBeNull();
    expect(clientActionSummaryKey({})).toBeNull();
    expect(clientActionSummaryKey(undefined)).toBeNull();
  });
});
