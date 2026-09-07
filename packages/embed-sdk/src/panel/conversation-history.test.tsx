// @vitest-environment happy-dom
/**
 * The narrow panel's history browser. What it guards: the header button drops
 * the conversation list as a popover over the thread's corner, picking a row
 * switches to that conversation and closes it, Escape and a click outside close
 * it too, and sidebar mode shows no button at all — its list is already
 * permanent, so two ways in would fight.
 * Deleting a row is guarded here too: it confirms first, calls DELETE, and puts
 * the row back when the server refuses. So is renaming one: the name becomes a
 * box in place, Enter PATCHes it, Escape leaves it alone.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos } from '../core';
import { NannosProvider, type NannosHostAdapter } from '../react';
import { AssistantPanel } from './assistant-panel';

/** Radix ScrollArea and the thread's stick-to-bottom both need it. */
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** Socket fake mirroring engine.test.tsx: never connects, so nothing streams. */
class FakeSocket {
  connected = false;
  on() {
    return this;
  }
  emit() {}
  connect() {}
  disconnect() {}
}

const ADAPTER: NannosHostAdapter = {
  // Skip agent-URL discovery and the persisted-settings fetch.
  defaults: { agentUrl: 'http://agent', model: 'm1' },
  api: { getUserSettings: async () => null },
};

const CONVERSATIONS = [
  {
    conversation_id: 'conv-a',
    title: 'Campaign 42 pacing',
    last_message: 'Pacing looks healthy',
    last_message_at: '2026-08-25T10:00:00.000Z',
    status: 'active',
    metadata: {
      summary: 'Why campaign 42 under-delivered last week.',
      page_context: {
        key: '/campaigns/123',
        title: 'Campaign 42',
        entity: { type: 'Campaign', id: '123', name: 'Summer sale' },
      },
    },
  },
  {
    conversation_id: 'conv-b',
    title: 'Invoice question',
    last_message: 'Sent to finance',
    last_message_at: '2026-08-24T10:00:00.000Z',
    status: 'active',
  },
];

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

function mountPanel(props: { showConversationList?: boolean; header?: ReactNode | false } = {}) {
  const core = createNannos({}, () => new FakeSocket() as unknown as Socket);
  render(
    <NannosProvider core={core}>
      <AssistantPanel shadow={false} adapter={ADAPTER} {...props} />
    </NannosProvider>,
  );
}

describe('conversation history', () => {
  let listFetches = 0;
  let deletes: string[] = [];
  let deleteStatus = 204;
  let patches: Array<{ path: string; title: unknown }> = [];
  let patchStatus = 204;

  beforeEach(() => {
    listFetches = 0;
    deletes = [];
    deleteStatus = 204;
    patches = [];
    patchStatus = 204;
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown, init?: RequestInit) => {
        const path = String(url);
        if (path.includes('/api/v1/conversations/') && init?.method === 'DELETE') {
          deletes.push(new URL(path).pathname);
          return new Response(null, { status: deleteStatus });
        }
        if (path.includes('/api/v1/conversations/') && init?.method === 'PATCH') {
          patches.push({
            path: new URL(path).pathname,
            title: JSON.parse(String(init.body)).title,
          });
          return new Response(null, { status: patchStatus });
        }
        if (path.includes('/api/v1/conversations/')) {
          listFetches += 1;
          return json({ conversations: CONVERSATIONS });
        }
        if (path.includes('/api/v1/messages/')) return json({ messages: [], next_cursor: null });
        return json({});
      }),
    );
  });

  // No vitest `globals`, so testing-library's auto-cleanup never runs: unmount
  // between cases or every query sees the previous panel too.
  afterEach(cleanup);

  it('opens from the header, switches conversation on a pick, and closes itself', async () => {
    mountPanel();

    const button = await screen.findByRole('button', { name: 'History' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(button);
    const dialog = await screen.findByRole('dialog', { name: 'Conversations' });
    expect(button.getAttribute('aria-expanded')).toBe('true');
    // Opened on purpose → the caret waits in the search box.
    expect(document.activeElement).toBe(screen.getByLabelText('Search conversations…'));
    // Opening refetches: the list moves on while the panel sits on one thread.
    await waitFor(() => expect(listFetches).toBeGreaterThan(1));

    const row = await within(dialog).findByText('Invoice question');
    fireEvent.click(row);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(dialog.isConnected).toBe(false);
    // The picked conversation is now the active one: the header takes its name…
    const header = document.querySelector('[data-slot="nannos-panel-header"]')!;
    await waitFor(() => expect(header.textContent).toContain('Invoice question'));
    // …and its row is marked current when the list comes back.
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    const reopened = await screen.findByRole('dialog', { name: 'Conversations' });
    await waitFor(() => {
      const current = within(reopened).getByText('Invoice question').closest('button');
      expect(current?.getAttribute('aria-current')).toBe('true');
    });
  });

  it('the header names the conversation, and an unnamed one reads as New conversation', async () => {
    mountPanel();
    const header = document.querySelector('[data-slot="nannos-panel-header"]')!;
    // A fresh panel starts a conversation, which has no name yet. The label comes
    // from the strings table, so a host translation reaches it (the store holds
    // no English of its own).
    await waitFor(() => expect(header.textContent).toContain('New conversation'));

    // Pick one out of history → the header takes its name.
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    const dialog = await screen.findByRole('dialog', { name: 'Conversations' });
    fireEvent.click(within(dialog).getByText('Campaign 42 pacing'));
    await waitFor(() => expect(header.textContent).toContain('Campaign 42 pacing'));

    // …and New chat gives the untitled label back.
    fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
    await waitFor(() => expect(header.textContent).toContain('New conversation'));
    expect(header.textContent).not.toContain('Campaign 42 pacing');
  });

  it('closes on a click outside', async () => {
    // The popover leaves the thread on screen, so a click out there has to mean
    // "put this away" — and nothing else: the backdrop swallows it.
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    await screen.findByRole('dialog', { name: 'Conversations' });

    const backdrop = document.querySelector(
      '[data-slot="nannos-conversation-history-backdrop"]',
    )!;
    fireEvent.pointerDown(backdrop);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('closes on Escape', async () => {
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    await screen.findByRole('dialog', { name: 'Conversations' });

    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('starting a new chat from the header closes the overlay too', async () => {
    // The overlay covers only the thread — the header stays visible above it,
    // so its new-chat button must not leave the list covering the fresh thread.
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    await screen.findByRole('dialog', { name: 'Conversations' });

    fireEvent.click(document.querySelector('[data-slot="nannos-panel-new-chat"]')!);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const header = document.querySelector('[data-slot="nannos-panel-header"]')!;
    await waitFor(() => expect(header.textContent).toContain('New conversation'));
  });

  it('a row shows what the conversation is about and where it started', async () => {
    // Scoped to the overlay: the empty thread behind it carries its own
    // "continue where you left off" card, naming the same conversation.
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    const overlay = await screen.findByRole('dialog', { name: 'Conversations' });

    // The backend's summary replaces the streamed last-message preview.
    await within(overlay).findByText('Why campaign 42 under-delivered last week.');
    expect(within(overlay).queryByText('Pacing looks healthy')).toBeNull();
    // The origin reads as the entity, with the route as the tooltip.
    const origin = await within(overlay).findByText('Campaign Summer sale');
    expect(origin.closest('[data-slot="nannos-conversation-origin"]')?.getAttribute('title')).toBe(
      '/campaigns/123',
    );

    // A row with neither falls back to its live preview and shows no origin.
    const plain = within(overlay).getByText('Invoice question').closest('button')!;
    expect(plain.querySelector('[data-slot="nannos-conversation-origin"]')).toBeNull();
  });

  it('leaves the untouched new conversation out of the list', async () => {
    mountPanel();
    // A fresh panel starts a conversation of its own. It has no name, no preview
    // and no summary, so its row would say only "New conversation" — which the
    // header already says. Only the server's rows belong in the history.
    const header = document.querySelector('[data-slot="nannos-panel-header"]')!;
    await waitFor(() => expect(header.textContent).toContain('New conversation'));

    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    const overlay = await screen.findByRole('dialog', { name: 'Conversations' });
    await within(overlay).findByText('Campaign 42 pacing');
    expect(within(overlay).queryByText('New conversation')).toBeNull();
    expect(overlay.querySelectorAll('[data-slot="nannos-conversation-item"]')).toHaveLength(2);
  });

  it('deleting a row confirms first, then DELETEs it and drops it from the list', async () => {
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    const overlay = await screen.findByRole('dialog', { name: 'Conversations' });
    const row = (await within(overlay).findByText('Invoice question')).closest('div.group\\/row')!;

    // The trash button lives NEXT TO the row button, not inside it — nested
    // buttons would be invalid markup.
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /Delete conversation/ }));
    expect(deletes).toEqual([]); // nothing happens until it is confirmed

    const confirm = await screen.findByRole('dialog', { name: 'Delete conversation?' });
    expect(confirm.textContent).toContain('Invoice question');
    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(deletes).toEqual(['/api/v1/conversations/conv-b']));
    await waitFor(() => expect(within(overlay).queryByText('Invoice question')).toBeNull());
    expect(within(overlay).getByText('Campaign 42 pacing')).toBeTruthy();
  });

  it('cancelling deletes nothing', async () => {
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    const overlay = await screen.findByRole('dialog', { name: 'Conversations' });
    const row = (await within(overlay).findByText('Invoice question')).closest('div.group\\/row')!;

    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /Delete conversation/ }));
    const confirm = await screen.findByRole('dialog', { name: 'Delete conversation?' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Delete conversation?' })).toBeNull(),
    );
    expect(deletes).toEqual([]);
    expect(within(overlay).getByText('Invoice question')).toBeTruthy();
  });

  it('a refused delete puts the row back', async () => {
    deleteStatus = 500;
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    const overlay = await screen.findByRole('dialog', { name: 'Conversations' });
    const row = (await within(overlay).findByText('Invoice question')).closest('div.group\\/row')!;

    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /Delete conversation/ }));
    const confirm = await screen.findByRole('dialog', { name: 'Delete conversation?' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(deletes).toHaveLength(1));
    // The optimistic removal is undone — the row is the user's only feedback.
    await waitFor(() => expect(within(overlay).getByText('Invoice question')).toBeTruthy());
  });

  it('renaming a row edits it in place and PATCHes the new name', async () => {
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    const overlay = await screen.findByRole('dialog', { name: 'Conversations' });
    const row = (await within(overlay).findByText('Invoice question')).closest('div.group\\/row')!;

    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /Rename conversation/ }));

    // The name becomes a box, prefilled — most renames correct the old name
    // rather than replace it — while the line under it stays put.
    const box = within(row as HTMLElement).getByLabelText('Conversation name') as HTMLInputElement;
    expect(box.value).toBe('Invoice question');
    expect(within(row as HTMLElement).getByText('Sent to finance')).toBeTruthy();

    fireEvent.change(box, { target: { value: 'Invoice for August' } });
    fireEvent.keyDown(box, { key: 'Enter' });

    await waitFor(() =>
      expect(patches).toEqual([
        { path: '/api/v1/conversations/conv-b', title: 'Invoice for August' },
      ]),
    );
    // Back to a row, under its new name.
    await waitFor(() => expect(within(overlay).getByText('Invoice for August')).toBeTruthy());
    expect(within(overlay).queryByLabelText('Conversation name')).toBeNull();
  });

  it('Escape leaves the name alone', async () => {
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    const overlay = await screen.findByRole('dialog', { name: 'Conversations' });
    const row = (await within(overlay).findByText('Invoice question')).closest('div.group\\/row')!;

    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /Rename conversation/ }));
    const box = within(row as HTMLElement).getByLabelText('Conversation name');
    fireEvent.change(box, { target: { value: 'Something else' } });
    fireEvent.keyDown(box, { key: 'Escape' });

    await waitFor(() => expect(within(overlay).getByText('Invoice question')).toBeTruthy());
    expect(patches).toEqual([]);
  });

  it('a refused rename puts the old name back', async () => {
    patchStatus = 500;
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    mountPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'History' }));
    const overlay = await screen.findByRole('dialog', { name: 'Conversations' });
    const row = (await within(overlay).findByText('Invoice question')).closest('div.group\\/row')!;

    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /Rename conversation/ }));
    const box = within(row as HTMLElement).getByLabelText('Conversation name');
    fireEvent.change(box, { target: { value: 'Invoice for August' } });
    fireEvent.keyDown(box, { key: 'Enter' });

    await waitFor(() => expect(patches).toHaveLength(1));
    await waitFor(() => expect(within(overlay).getByText('Invoice question')).toBeTruthy());
  });

  it('sidebar mode offers no history button — the list is already on screen', async () => {
    mountPanel({ showConversationList: true });

    await screen.findByText('Campaign 42 pacing');
    expect(screen.queryByRole('button', { name: 'History' })).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('sidebar mode moves new-chat INTO the list — the header keeps none', async () => {
    // Same rule as the history button: one way in. With the list permanently on
    // screen, the button belongs where the user is already reading their chats.
    mountPanel({ showConversationList: true });
    await screen.findByText('Campaign 42 pacing');

    const list = document.querySelector('[data-slot="nannos-conversation-list"]')!;
    expect(list.querySelector('[data-slot="nannos-conversation-new-chat"]')).toBeTruthy();
    expect(document.querySelector('[data-slot="nannos-panel-new-chat"]')).toBeNull();
  });

  it('a headerless sidebar surface can still start a new chat', async () => {
    // The console's chat page: `header={false}` + sidebar. Before the list
    // carried the button, this surface had NO way to start a conversation.
    mountPanel({ showConversationList: true, header: false });
    await screen.findByText('Campaign 42 pacing');
    expect(document.querySelector('[data-slot="nannos-panel-header"]')).toBeNull();

    // Open one from the list, so there is something for new-chat to move off.
    fireEvent.click(screen.getByText('Campaign 42 pacing'));
    const row = () => document.querySelector('[data-slot="nannos-conversation-item"][aria-current]');
    await waitFor(() => expect(row()).toBeTruthy());

    fireEvent.click(document.querySelector('[data-slot="nannos-conversation-new-chat"]')!);

    // The fresh conversation is not in the list yet (nothing has happened in
    // it), so no row is the current one any more.
    await waitFor(() => expect(row()).toBeNull());
  });

  it('the narrow panel keeps its header new-chat button', async () => {
    mountPanel();
    await screen.findByRole('button', { name: 'History' });
    expect(document.querySelector('[data-slot="nannos-panel-new-chat"]')).toBeTruthy();
  });
});
