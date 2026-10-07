import { describe, expect, it } from 'vitest';
import { NannosCore } from './index';

const core = () =>
  new NannosCore({ backendUrl: 'http://x', agentUrl: 'http://y' }, () => ({
    connect: () => {},
    disconnect: () => {},
    on: () => () => {},
    emit: () => true,
    get connected() {
      return false;
    },
  }) as never);

function form(c: NannosCore) {
  let state: Record<string, unknown> = { name: '' };
  return c.register({
    type: 'Job',
    id: 'new',
    scope: 'create',
    getState: () => state,
    apply: (patch) => {
      state = { ...state, ...patch };
    },
  });
}

// What the assistant's fill leaves behind: a mark with its undo.
const mark = (c: NannosCore) =>
  c.changes.record({ type: 'Job', id: 'new' }, { name: '' }, ['name'], async (v) => Object.keys(v));

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('change marks across a re-registration', () => {
  it('keep their marks when the form is disposed and registered again in one go', async () => {
    const c = core();
    const first = form(c);
    mark(c);
    expect(c.changes.pending()).toHaveLength(1);
    // A host effect re-running: cleanup disposes, setup registers the same key again.
    first.dispose();
    form(c);
    await settle();
    expect(c.changes.pending()).toHaveLength(1);
  });

  it('go away when the form is swapped for a view of the same object', async () => {
    // Leaving edit mode (cancel, the user's own Save) replaces the form with a view
    // registration under the same type:id in one commit.
    const c = core();
    const handle = form(c);
    mark(c);
    handle.dispose();
    c.register({ type: 'Job', id: 'new', scope: 'view', getState: () => ({}), apply: () => {} });
    await settle();
    expect(c.changes.pending()).toEqual([]);
  });

  it('go away with a form that leaves the screen', async () => {
    const c = core();
    const handle = form(c);
    mark(c);
    handle.dispose();
    await settle();
    expect(c.changes.pending()).toEqual([]);
  });
});

describe('undo', () => {
  it('reports a write that throws as a failed undo and keeps the mark', async () => {
    const c = core();
    const [change] = c.changes.record({ type: 'Job', id: 'new' }, { name: '' }, ['name'], async () => {
      throw new Error('bridge write failed');
    });
    expect(await change.undo()).toBe(false);
    expect(c.changes.pending()).toHaveLength(1);
  });
});
