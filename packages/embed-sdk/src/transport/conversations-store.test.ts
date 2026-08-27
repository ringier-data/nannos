// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationsStore, MAX_CONVERSATION_TITLE } from './conversations-store';

function fetchReturning(items: unknown[]): {
  fetch: (path: string) => Promise<Response>;
  calls: string[];
} {
  const calls: string[] = [];
  return {
    calls,
    fetch: async (path: string) => {
      calls.push(path);
      return new Response(JSON.stringify({ items }), { status: 200 });
    },
  };
}

/** List once, then record every DELETE and answer with `deleteStatus`. */
function fetchWithDelete(items: unknown[], deleteStatus = 204) {
  const deletes: string[] = [];
  return {
    deletes,
    fetch: async (path: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        deletes.push(path);
        return new Response(null, { status: deleteStatus });
      }
      return new Response(JSON.stringify({ items }), { status: 200 });
    },
  };
}

/** List once, then record every PATCH body and answer with `patchStatus`. */
function fetchWithPatch(items: unknown[], patchStatus = 204) {
  const patches: Array<{ path: string; title: unknown }> = [];
  return {
    patches,
    fetch: async (path: string, init?: RequestInit) => {
      if (init?.method === 'PATCH') {
        patches.push({ path, title: JSON.parse(String(init.body)).title });
        return new Response(null, { status: patchStatus });
      }
      return new Response(JSON.stringify({ items }), { status: 200 });
    },
  };
}

const serverConv = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  title: `Conv ${id}`,
  last_message: 'hi',
  last_message_at: '2026-08-25T10:00:00Z',
  ...extra,
});

beforeEach(() => sessionStorage.clear());

describe('ConversationsStore', () => {
  it('list mapping + console auto-select adopts the most recent', async () => {
    const { fetch, calls } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList();
    expect(store.getSnapshot().items.map((c) => c.id)).toEqual(['c1', 'c2']);
    expect(store.activeId).toBe('c1');
    expect(calls[0]).toContain('exclude_playground=true');
  });

  it('reads the summary and the origin the backend stamped', async () => {
    const { fetch } = fetchReturning([
      serverConv('c1', {
        metadata: {
          summary: 'Why campaign 42 under-delivered last week.',
          page_context: {
            key: '/campaigns/123',
            title: 'Campaign 42',
            entity: { type: 'Campaign', id: '123', name: 'Summer sale' },
          },
        },
      }),
    ]);
    const store = new ConversationsStore({ fetch });
    await store.loadList();
    const conversation = store.getSnapshot().items[0];
    expect(conversation.summary).toBe('Why campaign 42 under-delivered last week.');
    expect(conversation.origin).toEqual({
      key: '/campaigns/123',
      title: 'Campaign 42',
      entity: { type: 'Campaign', id: '123', name: 'Summer sale' },
    });
  });

  it('rows without the stamp carry neither field (older conversations)', async () => {
    const { fetch } = fetchReturning([
      serverConv('c1'),
      serverConv('c2', { metadata: { embedded_sub_agent_id: '42' } }),
      // Shapes an older or half-written stamp could leave behind.
      serverConv('c3', { metadata: { summary: '   ', page_context: { view: { tab: 'x' } } } }),
      serverConv('c4', { metadata: { page_context: 'not-an-object' } }),
    ]);
    const store = new ConversationsStore({ fetch });
    await store.loadList();
    for (const conversation of store.getSnapshot().items) {
      expect(conversation.summary).toBeUndefined();
      expect(conversation.origin).toBeUndefined();
    }
    expect(store.getSnapshot().items[1].embeddedSubAgentId).toBe('42');
  });

  it('keeps a partial origin — a page key with no entity is still worth showing', async () => {
    const { fetch } = fetchReturning([
      serverConv('c1', { metadata: { page_context: { key: '/reports' } } }),
    ]);
    const store = new ConversationsStore({ fetch });
    await store.loadList();
    expect(store.getSnapshot().items[0].origin).toEqual({ key: '/reports' });
  });

  it('a fresh conversation carries NO title — the UI owns the untitled label', async () => {
    const { fetch } = fetchReturning([]);
    const store = new ConversationsStore({ fetch });
    const id = store.create();
    expect(store.getSnapshot().items[0].title).toBe('');

    // The first user message names it locally…
    store.noteTitle(id, 'Why is campaign 42 under-delivering this week? Please check the caps');
    expect(store.getSnapshot().items[0].title).toBe('Why is campaign 42 under-delivering this…');

    // …and a second message does not rename it.
    store.noteTitle(id, 'something else');
    expect(store.getSnapshot().items[0].title).toBe('Why is campaign 42 under-delivering this…');
  });

  it('a pushed server title replaces the local one, and lands with the summary', async () => {
    const { fetch } = fetchReturning([]);
    const store = new ConversationsStore({ fetch });
    const id = store.create();
    store.noteTitle(id, 'why is campaign 42 under-delivering this week');

    store.applyServerTitle(id, {
      title: 'Campaign 42 pacing',
      summary: 'Why campaign 42 under-delivered last week.',
    });
    expect(store.getSnapshot().items[0].title).toBe('Campaign 42 pacing');
    expect(store.getSnapshot().items[0].summary).toBe('Why campaign 42 under-delivered last week.');
  });

  it('an empty or unknown push changes nothing', async () => {
    const { fetch } = fetchReturning([]);
    const store = new ConversationsStore({ fetch });
    const id = store.create();
    store.noteTitle(id, 'local title');
    let ticks = 0;
    store.subscribe(() => (ticks += 1));

    store.applyServerTitle(id, {});
    store.applyServerTitle(id, { title: '   ' });
    store.applyServerTitle('someone-elses-conversation', { title: 'Nope' });
    store.applyServerTitle(id, { title: 'local title' }); // same value
    expect(store.getSnapshot().items[0].title).toBe('local title');
    expect(ticks).toBe(0); // no pointless re-render of every subscriber
  });

  it('embedded scoping: subAgentId rides the query; resume only from session storage', async () => {
    sessionStorage.setItem(
      'nannos-active-conversation:42',
      JSON.stringify({ id: 'c2', contextKey: 'campaign:7' }),
    );
    const { fetch, calls } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, subAgentId: 42, autoSelectConversation: false });
    await store.loadList();
    expect(calls[0]).toContain('embedded_sub_agent_id=42');
    expect(store.activeId).toBe('c2'); // session resume, NOT most-recent
    expect(store.contextKeyOf('c2')).toBe('campaign:7');
  });

  it('a reload lands back on the conversation the tab was on, not the most recent', async () => {
    sessionStorage.setItem('nannos-active-conversation:default', JSON.stringify({ id: 'c2' }));
    const { fetch } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList();
    expect(store.activeId).toBe('c2'); // session resume beats most-recent (c1)
  });

  it('the resumed conversation replaces the blank one the panel minted meanwhile', async () => {
    sessionStorage.setItem('nannos-active-conversation:default', JSON.stringify({ id: 'c2' }));
    const { fetch } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    // The panel renders with nothing selected and adopts a minted id, which
    // normally beats the list fetch home.
    store.adopt('minted-1');
    await store.loadList();
    expect(store.activeId).toBe('c2');
    // The empty placeholder is gone — it never held anything.
    expect(store.getSnapshot().items.map((c) => c.id)).toEqual(['c1', 'c2']);
    expect(store.isLocalOnly('minted-1')).toBe(false);
  });

  it('resume never takes away a conversation the user already spoke into', async () => {
    sessionStorage.setItem('nannos-active-conversation:default', JSON.stringify({ id: 'c2' }));
    const { fetch } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    store.adopt('minted-1');
    store.noteTitle('minted-1', 'typed before the list arrived');
    await store.loadList();
    expect(store.activeId).toBe('minted-1');
  });

  it('resume is spent on the first list — a later refresh leaves the user alone', async () => {
    sessionStorage.setItem('nannos-active-conversation:default', JSON.stringify({ id: 'c2' }));
    const { fetch } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList();
    store.select('c1');
    await store.loadList(); // e.g. a background turn finished
    expect(store.activeId).toBe('c1');
  });

  it('a new chat is not undone by a list refresh that still remembers the old one', async () => {
    sessionStorage.setItem('nannos-active-conversation:default', JSON.stringify({ id: 'c2' }));
    const { fetch } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    const fresh = store.create();
    await store.loadList();
    expect(store.activeId).toBe(fresh);
  });

  it('every surface records its conversation, under its own scope', async () => {
    const { fetch } = fetchReturning([]);
    const console_ = new ConversationsStore({ fetch, autoSelectConversation: true });
    console_.adopt('console-conv');
    const playground = new ConversationsStore({ fetch, subAgentConfigHash: 'abc123' });
    playground.adopt('playground-conv');
    expect(JSON.parse(sessionStorage.getItem('nannos-active-conversation:default')!)).toMatchObject({
      id: 'console-conv',
    });
    expect(
      JSON.parse(sessionStorage.getItem('nannos-active-conversation:playground:abc123')!),
    ).toMatchObject({ id: 'playground-conv' });
  });

  it('a recorded conversation the server no longer lists falls back to most-recent', async () => {
    sessionStorage.setItem('nannos-active-conversation:default', JSON.stringify({ id: 'deleted' }));
    const { fetch } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList();
    expect(store.activeId).toBe('c1');
  });

  it('no session record → embedded surface selects nothing (fresh start)', async () => {
    const { fetch } = fetchReturning([serverConv('c1')]);
    const store = new ConversationsStore({ fetch, subAgentId: 42, autoSelectConversation: false });
    await store.loadList();
    expect(store.activeId).toBeNull();
  });

  it('a locally created conversation survives a list refresh that does not know it', async () => {
    const { fetch } = fetchReturning([serverConv('c1')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    const localId = store.create('campaign:9');
    await store.loadList();
    expect(store.getSnapshot().items.map((c) => c.id)).toContain(localId);
    expect(store.activeId).toBe(localId);
  });

  it('resolveTarget: same contextKey continues, a different one starts fresh', async () => {
    const { fetch } = fetchReturning([]);
    const store = new ConversationsStore({ fetch });
    const a = store.create('campaign:A');
    expect(store.resolveTarget('campaign:A')).toBe(a); // same key → continue
    expect(store.resolveTarget(undefined)).toBe(a); // unkeyed → continue
    const b = store.resolveTarget('campaign:B'); // new key → fresh
    expect(b).not.toBe(a);
    expect(store.activeId).toBe(b);
  });

  it('resolveTarget fresh: always a new conversation, except a still-blank active one', async () => {
    const { fetch } = fetchReturning([]);
    const store = new ConversationsStore({ fetch });
    const a = store.create('campaign:A');
    // Blank active → reused, so a re-rendering drain effect settles instead of looping.
    expect(store.resolveTarget('campaign:A', { fresh: true })).toBe(a);
    store.noteTitle(a, 'How is it performing?');
    // Written to → same key no longer continues; fresh means fresh.
    const b = store.resolveTarget('campaign:A', { fresh: true });
    expect(b).not.toBe(a);
    expect(store.activeId).toBe(b);
  });

  it('read-only for conversations owned by ANOTHER embedded surface', async () => {
    const { fetch } = fetchReturning([
      serverConv('mine', { metadata: { embedded_sub_agent_id: 42 } }),
      serverConv('theirs', { metadata: { embedded_sub_agent_id: 7 } }),
      serverConv('console-one'),
    ]);
    const store = new ConversationsStore({ fetch, subAgentId: 42, autoSelectConversation: false });
    await store.loadList();
    expect(store.isReadOnly('mine')).toBe(false);
    expect(store.isReadOnly('theirs')).toBe(true);
    expect(store.isReadOnly('console-one')).toBe(false);
  });

  it('unread counts: activity on a background conversation increments; selecting clears', async () => {
    const { fetch } = fetchReturning([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList(); // active = c1
    store.noteActivity('c2', 'new answer text');
    expect(store.getSnapshot().items.find((c) => c.id === 'c2')).toMatchObject({
      unread: 1,
      lastMessage: 'new answer text',
    });
    store.noteActivity('c1', 'active convo'); // active: never unread
    expect(store.getSnapshot().items.find((c) => c.id === 'c1')?.unread).toBe(0);
    store.select('c2');
    expect(store.getSnapshot().items.find((c) => c.id === 'c2')?.unread).toBe(0);
  });

  it('adopt registers a render-minted id once and persists the session record', async () => {
    const { fetch } = fetchReturning([]);
    const store = new ConversationsStore({ fetch, subAgentId: 42, autoSelectConversation: false });
    const listener = vi.fn();
    store.subscribe(listener);
    store.adopt('minted-1');
    store.adopt('minted-1'); // idempotent
    expect(store.activeId).toBe('minted-1');
    expect(store.getSnapshot().items).toHaveLength(1);
    expect(JSON.parse(sessionStorage.getItem('nannos-active-conversation:42')!)).toMatchObject({
      id: 'minted-1',
    });
  });

  it('first user message titles a new conversation, truncated', async () => {
    const { fetch } = fetchReturning([]);
    const store = new ConversationsStore({ fetch });
    const id = store.create();
    store.noteTitle(id, 'a'.repeat(60));
    expect(store.getSnapshot().items[0].title).toBe('a'.repeat(40) + '…');
    store.noteTitle(id, 'second message'); // only the FIRST titles it
    expect(store.getSnapshot().items[0].title).toBe('a'.repeat(40) + '…');
  });

  it('remove deletes server-side and drops the row', async () => {
    const { fetch, deletes } = fetchWithDelete([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList(); // active = c1

    expect(await store.remove('c2')).toBe(true);
    expect(deletes).toEqual(['/api/v1/conversations/c2']);
    expect(store.getSnapshot().items.map((c) => c.id)).toEqual(['c1']);
    expect(store.activeId).toBe('c1'); // untouched: c2 was not the active one
  });

  it('removing the ACTIVE conversation lands on a fresh chat, never on nothing', async () => {
    const { fetch } = fetchWithDelete([serverConv('c1'), serverConv('c2')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList(); // active = c1

    expect(await store.remove('c1')).toBe(true);
    // A null activeId would let useNannosChat re-adopt its minted id — which is
    // the row we just deleted.
    expect(store.activeId).not.toBeNull();
    expect(store.activeId).not.toBe('c1');
    expect(store.isLocalOnly(store.activeId!)).toBe(true);
    expect(store.getSnapshot().items.map((c) => c.id)).toEqual([store.activeId, 'c2']);
  });

  it('a local-only conversation is dropped without touching the server', async () => {
    const { fetch, deletes } = fetchWithDelete([]);
    const store = new ConversationsStore({ fetch });
    const id = store.create();
    expect(await store.remove(id)).toBe(true);
    expect(deletes).toEqual([]);
    expect(store.getSnapshot().items.some((c) => c.id === id)).toBe(false);
  });

  it('404 means already gone — the row stays deleted', async () => {
    const { fetch } = fetchWithDelete([serverConv('c1'), serverConv('c2')], 404);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList();
    expect(await store.remove('c2')).toBe(true);
    expect(store.getSnapshot().items.map((c) => c.id)).toEqual(['c1']);
  });

  it('a refused delete puts the row back, in place and still selected', async () => {
    const { fetch } = fetchWithDelete([serverConv('c1'), serverConv('c2')], 500);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList(); // active = c1
    vi.spyOn(console, 'warn').mockImplementation(() => {});

    expect(await store.remove('c1')).toBe(false);
    expect(store.getSnapshot().items.map((c) => c.id)).toEqual(['c1', 'c2']);
    expect(store.activeId).toBe('c1');
    // The replacement chat minted for the optimistic update is gone with it.
    expect(store.getSnapshot().items).toHaveLength(2);
  });

  it('rename stores the new name and shows it at once', async () => {
    const { fetch, patches } = fetchWithPatch([serverConv('c1')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList();

    expect(await store.rename('c1', '  Q3   pacing  ')).toBe(true);
    // Cleaned exactly as the endpoint cleans it, so the row shows what was
    // actually stored.
    expect(patches).toEqual([{ path: '/api/v1/conversations/c1', title: 'Q3 pacing' }]);
    expect(store.getSnapshot().items[0].title).toBe('Q3 pacing');
  });

  it('rename caps the name at what the endpoint accepts', async () => {
    const { fetch, patches } = fetchWithPatch([serverConv('c1')]);
    const store = new ConversationsStore({ fetch });
    await store.loadList();

    await store.rename('c1', 'x'.repeat(200));
    expect(patches[0].title).toBe('x'.repeat(MAX_CONVERSATION_TITLE));
  });

  it('a refused rename puts the old name back', async () => {
    const { fetch } = fetchWithPatch([serverConv('c1')], 500);
    const store = new ConversationsStore({ fetch });
    await store.loadList();
    vi.spyOn(console, 'warn').mockImplementation(() => {});

    expect(await store.rename('c1', 'Q3 pacing')).toBe(false);
    expect(store.getSnapshot().items[0].title).toBe('Conv c1');
  });

  it('a refused rename keeps activity that landed while it was in flight', async () => {
    const { fetch } = fetchWithPatch([serverConv('c1')], 500);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList();
    store.select('c2'); // c1 is now a background conversation
    vi.spyOn(console, 'warn').mockImplementation(() => {});

    const pending = store.rename('c1', 'Q3 pacing');
    store.noteActivity('c1', 'a new answer');
    expect(await pending).toBe(false);

    const row = store.getSnapshot().items.find((c) => c.id === 'c1')!;
    expect(row.title).toBe('Conv c1'); // only the name was rolled back
    expect(row.lastMessage).toBe('a new answer');
    expect(row.unread).toBe(1);
  });

  it('a conversation with no server row yet keeps the name it was given', async () => {
    // 404 is expected here — nothing has been sent in it, so the first turn is
    // what creates the row. Reverting would throw away what the user typed.
    const { fetch } = fetchWithPatch([], 404);
    const store = new ConversationsStore({ fetch });
    const id = store.create();

    expect(await store.rename(id, 'Q3 pacing')).toBe(true);
    expect(store.getSnapshot().items[0].title).toBe('Q3 pacing');
  });

  it('renaming to the same name, an empty name, or an unknown id sends nothing', async () => {
    const { fetch, patches } = fetchWithPatch([serverConv('c1')]);
    const store = new ConversationsStore({ fetch });
    await store.loadList();

    expect(await store.rename('c1', 'Conv c1')).toBe(true); // nothing to do
    expect(await store.rename('c1', '   ')).toBe(false); // no way back to a name
    expect(await store.rename('nope', 'Q3 pacing')).toBe(false);
    expect(patches).toEqual([]);
  });

  it('removing an unknown id is a no-op', async () => {
    const { fetch, deletes } = fetchWithDelete([serverConv('c1')]);
    const store = new ConversationsStore({ fetch, autoSelectConversation: true });
    await store.loadList();
    expect(await store.remove('nope')).toBe(false);
    expect(deletes).toEqual([]);
    expect(store.getSnapshot().items).toHaveLength(1);
  });
});
