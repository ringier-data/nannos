import { describe, expect, it, vi } from 'vitest';
import { executeClientAction, extractClientActionDirective } from './client-action';
import { CLIENT_ACTION_EXT } from './extensions';
import { ObjectRegistry } from './registry';

function registryWithCampaign(apply = vi.fn()) {
  const r = new ObjectRegistry();
  r.register({
    type: 'Campaign',
    id: '123',
    scope: 'update',
    getState: () => ({}),
    apply,
  });
  return { r, apply };
}

describe('executeClientAction (Zod-guarded boundary)', () => {
  it('rejects payloads that are not a well-formed directive', async () => {
    const { r } = registryWithCampaign();
    const res = await executeClientAction({ kind: 'frobnicate' }, { registry: r });
    expect(res).toEqual({ ok: false, reason: 'invalid' });
  });

  it('refuses apply against an unregistered target (never guesses)', async () => {
    const { r } = registryWithCampaign();
    const res = await executeClientAction(
      { kind: 'apply', target: { type: 'Campaign', id: 'other' }, values: { name: 'x' } },
      { registry: r },
    );
    expect(res).toEqual({ ok: false, reason: 'unknown-target' });
  });

  it('applies values through the registered handle', async () => {
    const { r, apply } = registryWithCampaign();
    const res = await executeClientAction(
      { kind: 'apply', target: { type: 'Campaign', id: '123' }, values: { name: 'Spring' } },
      { registry: r },
    );
    expect(res).toEqual({ ok: true });
    expect(apply).toHaveBeenCalledWith({ name: 'Spring' });
  });

  it('applies directly and ignores the directive `confirm` field (HITL is upstream)', async () => {
    // The SDK no longer has a confirm layer — approval for an apply happens once at
    // the agent's tool-call HITL gate (client_action is risk-scored by kind). A
    // directive reaching the SDK is pre-approved, so `confirm: true` is ignored.
    const { r, apply } = registryWithCampaign();
    const res = await executeClientAction(
      { kind: 'apply', target: { type: 'Campaign', id: '123' }, values: { name: 'x' }, confirm: true },
      { registry: r },
    );
    expect(res).toEqual({ ok: true });
    expect(apply).toHaveBeenCalledWith({ name: 'x' });
  });

  it('surfaces an ApplyResult with rejections to onApplyResult (not silent)', async () => {
    const apply = vi.fn(() => ({ applied: ['name'], rejected: [{ field: 'status', reason: 'failed schema validation' }] }));
    const { r } = registryWithCampaign(apply);
    const onApplyResult = vi.fn();
    const res = await executeClientAction(
      { kind: 'apply', target: { type: 'Campaign', id: '123' }, values: { name: 'x', status: 'bogus' } },
      { registry: r, onApplyResult },
    );
    expect(res).toEqual({ ok: true, applied: ['name'], rejected: [{ field: 'status', reason: 'failed schema validation' }] });
    expect(onApplyResult).toHaveBeenCalledWith(
      { type: 'Campaign', id: '123' },
      { applied: ['name'], rejected: [{ field: 'status', reason: 'failed schema validation' }] },
    );
  });

  it('warns (never silent) on rejections when no onApplyResult is wired', async () => {
    const apply = vi.fn(() => ({ applied: [], rejected: [{ field: 'status' }] }));
    const { r } = registryWithCampaign(apply);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await executeClientAction(
      { kind: 'apply', target: { type: 'Campaign', id: '123' }, values: { status: 'bogus' } },
      { registry: r },
    );
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it('routes navigate to the host hook', async () => {
    const { r } = registryWithCampaign();
    const navigate = vi.fn();
    const res = await executeClientAction({ kind: 'navigate', to: '/campaigns/123' }, { registry: r, navigate });
    expect(res).toEqual({ ok: true });
    expect(navigate).toHaveBeenCalledWith('/campaigns/123');
  });
});

describe('async apply handles', () => {
  it('awaits an async apply handle instead of mistaking the Promise for an ApplyResult', async () => {
    const apply = vi.fn(async () => ({
      applied: ['name'],
      rejected: [{ field: 'status', reason: 'failed schema validation' }],
    }));
    const { r } = registryWithCampaign(apply);
    const onApplyResult = vi.fn();
    const res = await executeClientAction(
      { kind: 'apply', target: { type: 'Campaign', id: '123' }, values: { name: 'x', status: 'bogus' } },
      { registry: r, onApplyResult },
    );
    expect(res).toEqual({
      ok: true,
      applied: ['name'],
      rejected: [{ field: 'status', reason: 'failed schema validation' }],
    });
    expect(onApplyResult).toHaveBeenCalled();
  });

  it('tolerates a non-ApplyResult truthy return from a custom handle', async () => {
    const apply = vi.fn(() => 'done' as never);
    const { r } = registryWithCampaign(apply);
    const res = await executeClientAction(
      { kind: 'apply', target: { type: 'Campaign', id: '123' }, values: { name: 'x' } },
      { registry: r },
    );
    expect(res).toEqual({ ok: true });
  });
});

describe('extractClientActionDirective (envelope demux)', () => {
  const directive = { kind: 'navigate', to: '/campaigns/123' };
  const envelope = {
    kind: 'status-update',
    status: {
      message: {
        extensions: [CLIENT_ACTION_EXT],
        parts: [{ kind: 'data', data: { directive } }],
      },
    },
  };

  it('unwraps the directive from a tagged status-update event', () => {
    expect(extractClientActionDirective(envelope)).toEqual(directive);
  });

  it('returns null for untagged status-updates, other event kinds, and bare directives', () => {
    expect(
      extractClientActionDirective({
        kind: 'status-update',
        status: { message: { extensions: [], parts: [{ kind: 'text', text: 'chunk' }] } },
      }),
    ).toBeNull();
    expect(extractClientActionDirective({ kind: 'artifact-update' })).toBeNull();
    expect(extractClientActionDirective(directive)).toBeNull();
    expect(extractClientActionDirective(null)).toBeNull();
  });
});
