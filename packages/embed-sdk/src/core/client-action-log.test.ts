import { describe, expect, it, vi } from 'vitest';
import { ClientActionLog } from './client-action-log';
import { NannosCore } from './index';
import { ObjectRegistry } from './registry';

describe('ClientActionLog', () => {
  it('describes an unvalidated directive and settles it', () => {
    const log = new ClientActionLog();
    const id = log.start(
      'round-trip',
      { kind: 'apply', target: { type: 'campaign', id: '1' }, values: { name: 'x' } },
      ['campaign:1'],
    );
    expect(log.getSnapshot()[0]).toMatchObject({
      path: 'round-trip',
      kind: 'apply',
      target: 'campaign:1',
      outcome: 'pending',
      knownTargets: ['campaign:1'],
    });
    log.settle(id, { ok: true, applied: ['name'], rejected: [] });
    const entry = log.getSnapshot()[0];
    expect(entry.outcome).toBe('ok');
    expect(entry.durationMs).toBeGreaterThanOrEqual(0);
  });

  it('rows a garbled directive rather than dropping it', () => {
    const log = new ClientActionLog();
    const id = log.start('fire-and-forget', 'not-an-object', []);
    expect(log.getSnapshot()[0]).toMatchObject({ kind: null, target: null });
    log.settle(id, { ok: false, reason: 'invalid' });
    expect(log.getSnapshot()[0].outcome).toBe('refused');
  });

  it('marks a thrown host handler apart from a refusal', () => {
    const log = new ClientActionLog();
    const id = log.start('round-trip', { kind: 'apply' }, []);
    log.fail(id, new Error('form unmounted'));
    expect(log.getSnapshot()[0]).toMatchObject({ outcome: 'threw', error: 'form unmounted' });
  });

  it('notifies subscribers and hands back a stable snapshot between changes', () => {
    const log = new ClientActionLog();
    const seen = vi.fn();
    log.subscribe(seen);
    const before = log.getSnapshot();
    expect(log.getSnapshot()).toBe(before);
    log.start('round-trip', { kind: 'navigate', to: '/' }, []);
    expect(seen).toHaveBeenCalledTimes(1);
    expect(log.getSnapshot()).not.toBe(before);
  });

  it('caps the buffer and ignores a settle for an evicted entry', () => {
    const log = new ClientActionLog();
    const first = log.start('round-trip', { kind: 'navigate', to: '/0' }, []);
    for (let i = 1; i < 60; i++) log.start('round-trip', { kind: 'navigate', to: `/${i}` }, []);
    expect(log.getSnapshot()).toHaveLength(50);
    log.settle(first, { ok: true });
    expect(log.getSnapshot()).toHaveLength(50);
    expect(log.getSnapshot().some((e) => e.id === first)).toBe(false);
  });

  it('clears', () => {
    const log = new ClientActionLog();
    log.start('round-trip', { kind: 'navigate', to: '/' }, []);
    log.clear();
    expect(log.getSnapshot()).toEqual([]);
  });
});

describe('ObjectRegistry.keys', () => {
  it('lists registered targets without reading host state', () => {
    const registry = new ObjectRegistry();
    const getState = vi.fn(() => ({}));
    registry.register({ type: 'campaign', id: '1', scope: 'read', getState, apply: () => {} });
    expect(registry.keys()).toEqual(['campaign:1']);
    expect(getState).not.toHaveBeenCalled();
  });
});

describe('NannosCore.runClientAction logging', () => {
  const core = () => new NannosCore({ backendUrl: 'http://x', agentUrl: 'http://y' }, () => ({
    connect: () => {},
    disconnect: () => {},
    on: () => () => {},
    emit: () => true,
    get connected() {
      return false;
    },
  }) as never);

  it('logs a refused target together with what WAS registered', async () => {
    const c = core();
    c.register({ type: 'campaign', id: '1', scope: 'read', getState: () => ({}), apply: () => {} });
    const result = await c.runClientAction({
      kind: 'apply',
      target: { type: 'campaign', id: 'other' },
      values: { a: 1 },
    });
    expect(result).toEqual({ ok: false, reason: 'unknown-target' });
    expect(c.clientActions.getSnapshot()[0]).toMatchObject({
      path: 'round-trip',
      kind: 'apply',
      target: 'campaign:other',
      outcome: 'refused',
      knownTargets: ['campaign:1'],
    });
  });

  it('logs a throwing host apply as `threw`', async () => {
    const c = core();
    c.register({
      type: 'campaign',
      id: '1',
      scope: 'write',
      getState: () => ({}),
      apply: () => {
        throw new Error('boom');
      },
    });
    await c.runClientAction({ kind: 'apply', target: { type: 'campaign', id: '1' }, values: { a: 1 } });
    expect(c.clientActions.getSnapshot()[0]).toMatchObject({ outcome: 'threw', error: 'boom' });
  });
});
