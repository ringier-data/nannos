import { describe, expect, it, vi } from 'vitest';
import { executeClientAction, extractClientActionDirective } from './client-action';
import { CLIENT_ACTION_EXT } from './extensions';
import { ObjectRegistry } from './registry';
import { ChangeStore } from './change-store';

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
    expect(res.ok).toBe(true);
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

describe('saving and changed-field marks', () => {
  const target = { type: 'Settings', id: 'me' };
  // A form's Save, as a form registration offers it: the approval-gated action `save`.
  function registry(save?: () => unknown) {
    const r = new ObjectRegistry();
    r.register({
      ...target,
      scope: 'update',
      getState: () => ({}),
      apply: () => ({ applied: ['language'], rejected: [] }),
      ...(save ? { actions: { save: { label: 'Save', requiresApproval: true, run: save as () => void } } } : {}),
    });
    return r;
  }
  const save = { kind: 'invoke', target, action: 'save' } as const;

  it('records what an apply wrote, with undo, and marks it', async () => {
    const state: Record<string, unknown> = { language: 'en' };
    const r = new ObjectRegistry();
    r.register({
      ...target,
      scope: 'update',
      includeValues: true,
      getState: () => ({ ...state }),
      apply: (v: Record<string, unknown>) => {
        Object.assign(state, v);
        return { applied: Object.keys(v), rejected: [] };
      },
    });
    const changes = new ChangeStore();
    const markChanged = vi.fn();
    const beforeApply = vi.fn(() => ({ language: 'English' }));
    const first = await executeClientAction(
      { kind: 'apply', target, values: { language: 'de' } },
      { registry: r, changes, beforeApply, markChanged },
    );
    // The agent is told what it overwrote: "undo that" is an apply of these values.
    expect(first).toMatchObject({ ok: true, previous: { language: 'en' } });
    expect(beforeApply).toHaveBeenCalledWith(target, ['language']);
    const [, recorded, captured] = markChanged.mock.calls[0];
    expect(captured).toEqual({ language: 'English' });
    expect(recorded.map((c: { field: string; previous: unknown }) => [c.field, c.previous])).toEqual([['language', 'en']]);
    expect(changes.get(target)).toHaveLength(1);

    // A second fill of the same field keeps the user's original value for undo.
    const second = await executeClientAction(
      { kind: 'apply', target, values: { language: 'fr' } },
      { registry: r, changes, markChanged },
    );
    expect(changes.get(target)[0].previous).toBe('en');
    expect(second).toMatchObject({ previous: { language: 'en' } });

    expect(await changes.get(target)[0].undo()).toBe(true);
    expect(state.language).toBe('en');
    expect(changes.get(target)).toHaveLength(0);
  });

  it('keeps the overwritten values from the agent when the host shares no values', async () => {
    const r = new ObjectRegistry();
    r.register({
      ...target,
      scope: 'update',
      getState: () => ({ language: 'en' }),
      apply: (v: Record<string, unknown>) => ({ applied: Object.keys(v), rejected: [] }),
    });
    const result = await executeClientAction(
      { kind: 'apply', target, values: { language: 'de' } },
      { registry: r, changes: new ChangeStore() },
    );
    expect(result.ok).toBe(true);
    expect(result).not.toHaveProperty('previous');
  });

  it('a fill back to the original value dismisses the mark (the agent undoing itself)', async () => {
    const state: Record<string, unknown> = { timezone: 'UTC' };
    const r = new ObjectRegistry();
    r.register({
      ...target,
      scope: 'update',
      includeValues: true,
      getState: () => ({ ...state }),
      apply: (v: Record<string, unknown>) => {
        Object.assign(state, v);
        return { applied: Object.keys(v), rejected: [] };
      },
    });
    const changes = new ChangeStore();
    await executeClientAction({ kind: 'apply', target, values: { timezone: 'Asia/Tokyo' } }, { registry: r, changes });
    expect(changes.get(target)).toHaveLength(1);
    const back = await executeClientAction({ kind: 'apply', target, values: { timezone: 'UTC' } }, { registry: r, changes });
    expect(back).toMatchObject({ ok: true, applied: ['timezone'], previous: { timezone: 'UTC' } });
    expect(state.timezone).toBe('UTC');
    expect(changes.get(target)).toHaveLength(0);
  });

  it("saves through the form's save action once approved, and clears the marks", async () => {
    const run = vi.fn(async () => undefined);
    const clearChanged = vi.fn();
    const changes = new ChangeStore();
    changes.record(target, { language: 'en' }, ['language'], async () => ['language']);
    const res = await executeClientAction(save, { registry: registry(run), clearChanged, changes, approved: true });
    expect(res).toMatchObject({ ok: true, saved: true });
    expect(changes.get(target)).toHaveLength(0);
    expect(run).toHaveBeenCalledOnce();
    expect(clearChanged).toHaveBeenCalledWith(target);
  });

  it('reports a refused save and keeps the marks', async () => {
    const clearChanged = vi.fn();
    const res = await executeClientAction(
      save,
      { registry: registry(() => ({ ok: false, detail: 'Name is required' })), clearChanged, approved: true },
    );
    expect(res).toEqual({ ok: false, reason: 'failed', detail: 'Name is required' });
    expect(clearChanged).not.toHaveBeenCalled();
  });

  it('refuses to save an object without a save action, naming what it offers', async () => {
    const res = await executeClientAction(save, { registry: registry(), approved: true });
    expect(res).toEqual({ ok: false, reason: 'unknown-action', detail: 'This object offers no actions.' });
  });

  it('says a save landed on a read-only view: no form open, nothing on screen', async () => {
    // Approved after a reload: the form had closed, and the agent still told the
    // user the fill was on screen and to press a Save button that did not exist.
    const r = new ObjectRegistry();
    r.register({
      type: 'SubAgent',
      id: '9',
      scope: 'view',
      getState: () => ({}),
      actions: { edit: { label: 'Edit', run: () => ({ ok: true }) } },
    });
    const res = await executeClientAction(
      { kind: 'invoke', target: { type: 'SubAgent', id: '9' }, action: 'save' },
      { registry: r, approved: true },
    );
    expect(res).toMatchObject({ ok: false, reason: 'unknown-action' });
    expect(res.ok === false && res.detail).toMatch(/no form is open, so nothing was saved.*offers: edit\./);
  });

  it('lists a form\'s save as an approval-gated action in the manifest', () => {
    expect(registry(() => true).manifest()[0].actions).toEqual([
      { name: 'save', label: 'Save', requiresApproval: true },
    ]);
    expect(registry().manifest()[0].actions).toBeUndefined();
  });
});

describe('navigate round trip', () => {
  it('waits for the new page to register, then reports it', async () => {
    const r = new ObjectRegistry();
    const navigate = vi.fn(() => {
      // The new route mounts its form a moment later.
      setTimeout(
        () =>
          r.register({ type: 'Settings', id: 'me', scope: 'update', label: 'Your settings', getState: () => ({}), apply: () => {} }),
        50,
      );
    });
    const res = await executeClientAction(
      { kind: 'navigate', to: '/app' },
      { registry: r, navigate, readCurrentPage: async () => ({ page: { key: '/app', title: 'Settings' } }) },
    );
    expect(navigate).toHaveBeenCalledWith('/app');
    expect(res.ok).toBe(true);
    const landed = JSON.parse((res as { content: string }).content);
    expect(landed.page).toEqual({ key: '/app', title: 'Settings' });
    expect(landed.objects.map((o: { type: string }) => o.type)).toEqual(['Settings']);
  });

  it('is unsupported without a host navigate', async () => {
    const res = await executeClientAction({ kind: 'navigate', to: '/x' }, { registry: new ObjectRegistry() });
    expect(res).toEqual({ ok: false, reason: 'unsupported' });
  });
});

describe('unchanged fields', () => {
  it('a field written with the value it already had is not recorded or marked', async () => {
    const r = new ObjectRegistry();
    r.register({
      type: 'Job', id: 'new', scope: 'create',
      getState: () => ({ schedule_kind: 'cron', name: '' }),
      apply: (v: Record<string, unknown>) => ({ applied: Object.keys(v), rejected: [] }),
    });
    const changes = new ChangeStore();
    const markChanged = vi.fn();
    await executeClientAction(
      { kind: 'apply', target: { type: 'Job', id: 'new' }, values: { schedule_kind: 'cron', name: 'Weekly' } },
      { registry: r, changes, markChanged },
    );
    expect(changes.get({ type: 'Job', id: 'new' }).map((c) => c.field)).toEqual(['name']);
  });
});


describe('undo restores without validation', () => {
  it('puts back an empty value the schema would refuse', async () => {
    const state: Record<string, unknown> = { name: '' };
    const r = new ObjectRegistry();
    r.register({
      type: 'Job', id: 'new', scope: 'create',
      getState: () => ({ ...state }),
      apply: (v: Record<string, unknown>) => {
        const ok = Object.keys(v).filter((k) => v[k] !== '');
        for (const k of ok) state[k] = v[k];
        return { applied: ok, rejected: Object.keys(v).filter((k) => v[k] === '').map((field) => ({ field })) };
      },
      restore: (v: Record<string, unknown>) => {
        Object.assign(state, v);
      },
    });
    const changes = new ChangeStore();
    await executeClientAction({ kind: 'apply', target: { type: 'Job', id: 'new' }, values: { name: 'Weekly' } }, { registry: r, changes });
    expect(await changes.get({ type: 'Job', id: 'new' })[0].undo()).toBe(true);
    expect(state.name).toBe('');
  });
});

describe('invoke', () => {
  function registryWithActions(run = vi.fn()) {
    const r = new ObjectRegistry();
    r.register({
      type: 'SubAgent',
      id: '9',
      scope: 'view',
      getState: () => ({}),
      apply: () => undefined,
      actions: { edit: { label: 'Edit configuration', run } },
    });
    return { r, run };
  }

  it('runs an action that saves only once the user approved it', async () => {
    // "Default low tier" saves on its own: the manifest says so, the agent's card asks,
    // and the browser refuses it when no approval came with it.
    const run = vi.fn();
    const r = new ObjectRegistry();
    r.register({
      type: 'GatewayModel',
      id: 'haiku',
      scope: 'update',
      getState: () => ({}),
      apply: () => undefined,
      actions: { set_tier_default: { label: 'Default low tier', requiresApproval: true, run } },
    });
    expect(r.manifest()[0].actions).toEqual([
      { name: 'set_tier_default', label: 'Default low tier', requiresApproval: true },
    ]);
    const directive = { kind: 'invoke', target: { type: 'GatewayModel', id: 'haiku' }, action: 'set_tier_default' } as const;

    const refused = await executeClientAction(directive, { registry: r });
    expect(refused).toMatchObject({ ok: false, reason: 'failed' });
    expect(run).not.toHaveBeenCalled();

    const ran = await executeClientAction(directive, { registry: r, approved: true });
    expect(ran).toMatchObject({ ok: true, saved: true });
    expect(run).toHaveBeenCalledOnce();
  });

  it('lists the actions in the manifest, without their handlers', () => {
    const { r } = registryWithActions();
    expect(r.manifest()[0].actions).toEqual([{ name: 'edit', label: 'Edit configuration' }]);
  });

  it('runs the action with its args and hands back the page it settled into', async () => {
    const { r, run } = registryWithActions();
    const res = await executeClientAction(
      { kind: 'invoke', target: { type: 'SubAgent', id: '9' }, action: 'edit', args: { section: 'model' } },
      { registry: r, readCurrentPage: async () => ({ page: { key: '/app/subagents/9' } }) },
    );
    expect(run).toHaveBeenCalledWith({ section: 'model' });
    expect(res.ok).toBe(true);
    expect(JSON.parse((res as { content: string }).content).page).toEqual({ key: '/app/subagents/9' });
  });

  it('refuses an action the object does not offer, naming the ones it does', async () => {
    const { r } = registryWithActions();
    const res = await executeClientAction(
      { kind: 'invoke', target: { type: 'SubAgent', id: '9' }, action: 'delete' },
      { registry: r },
    );
    expect(res).toEqual({ ok: false, reason: 'unknown-action', detail: 'This object offers: edit.' });
  });

  it('reports an action that could not run, with its reason', async () => {
    const { r } = registryWithActions(vi.fn(() => ({ ok: false, detail: 'not read-only' })));
    const res = await executeClientAction(
      { kind: 'invoke', target: { type: 'SubAgent', id: '9' }, action: 'edit' },
      { registry: r },
    );
    expect(res).toEqual({ ok: false, reason: 'failed', detail: 'not read-only' });
  });
});

describe('a host refusing a route', () => {
  it('reports the reason instead of a landed page', async () => {
    // With Admin Mode off the agent navigated to an admin page anyway and the route
    // guard bounced the user to Settings; the host now says no, and why.
    const navigate = vi.fn(() => 'Admin pages need Admin Mode on.');
    const res = await executeClientAction(
      { kind: 'navigate', to: '/app/admin/budget-guard' },
      { registry: new ObjectRegistry(), navigate },
    );
    expect(res).toEqual({ ok: false, reason: 'failed', detail: 'Admin pages need Admin Mode on.' });
  });
});

describe('navigate with unsaved assistant changes', () => {
  it('refuses to leave them behind unless the user said to discard', async () => {
    const r = new ObjectRegistry();
    const changes = new ChangeStore();
    changes.record({ type: 'SubAgent', id: '9' }, { model: 'tier:premium' }, ['model'], async (v) => Object.keys(v));
    const navigate = vi.fn();

    const refused = await executeClientAction({ kind: 'navigate', to: '/app' }, { registry: r, navigate, changes });
    expect(refused).toEqual({ ok: false, reason: 'unsaved-changes', detail: 'SubAgent:9 (model)' });
    expect(navigate).not.toHaveBeenCalled();

    const left = await executeClientAction(
      { kind: 'navigate', to: '/app', discard_changes: true },
      { registry: r, navigate, changes },
    );
    expect(left).toMatchObject({ ok: true, discarded: 'SubAgent:9 (model)' });
    expect(navigate).toHaveBeenCalledWith('/app');
  });

  it("also guards edits the user typed, listing a form once even when the agent filled it too", async () => {
    // The guard only knew the assistant's fills: a name the user typed was refreshed
    // over and would have been navigated away from without a word.
    const r = new ObjectRegistry();
    const base = { scope: 'update' as const, getState: () => ({}), apply: () => undefined };
    r.register({ ...base, type: 'Job', id: '1', isDirty: () => true });
    r.register({ ...base, type: 'SubAgent', id: '9', isDirty: () => true });
    r.register({ ...base, type: 'Clean', id: '2', isDirty: () => false });
    const changes = new ChangeStore();
    changes.record({ type: 'SubAgent', id: '9' }, { model: 'tier:premium' }, ['model'], async (v) => Object.keys(v));
    const navigate = vi.fn();

    const refused = await executeClientAction({ kind: 'navigate', to: '/app' }, { registry: r, navigate, changes });
    expect(refused).toEqual({
      ok: false,
      reason: 'unsaved-changes',
      detail: 'SubAgent:9 (model); Job:1 (edits typed by the user)',
    });
    expect(navigate).not.toHaveBeenCalled();
    expect(r.manifest().map((m) => [m.type, m.unsaved ?? false])).toEqual([
      ['Job', true],
      ['SubAgent', true],
      ['Clean', false],
    ]);
  });
});
