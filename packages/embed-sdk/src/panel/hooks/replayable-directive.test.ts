// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { replayableDirective } from './use-nannos-chat';

describe('replayableDirective', () => {
  afterEach(() => sessionStorage.clear());

  it('navigates once per request; a replay after a remount only reads the page', () => {
    const navigate = { kind: 'navigate', to: '/app/x' };
    expect(replayableDirective('a1', navigate)).toBe(navigate);
    expect(replayableDirective('a1', navigate)).toEqual({ kind: 'read_current_page' });
    expect(replayableDirective('a2', navigate)).toBe(navigate);
  });

  it('leaves every other kind replayable', () => {
    const apply = { kind: 'apply', target: { type: 'T', id: '1' }, values: {} };
    expect(replayableDirective('b1', apply)).toBe(apply);
    expect(replayableDirective('b1', apply)).toBe(apply);
  });
});
