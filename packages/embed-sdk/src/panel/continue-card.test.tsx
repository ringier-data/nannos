// @vitest-environment happy-dom
/**
 * "Continue where you left off" — the card at the top of an empty thread.
 * What it guards: the panel opens on a fresh chat, so the card names the most
 * recent conversation and reopens it on a click; it stays away when there is
 * no history to go back to, and it leaves as soon as a real conversation is on
 * screen.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  defaults: { agentUrl: 'http://agent', model: 'm1' },
  api: { getUserSettings: async () => null },
};

// Newest first, the way the endpoint returns them.
const CONVERSATIONS = [
  {
    conversation_id: 'conv-a',
    title: 'Campaign 42 pacing',
    last_message: 'Pacing looks healthy',
    last_message_at: '2026-08-25T10:00:00.000Z',
    status: 'active',
    metadata: { summary: 'Why campaign 42 under-delivered last week.' },
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

function mountPanel(conversations: unknown[], props: { showConversationList?: boolean } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const path = String(url);
      if (path.includes('/api/v1/conversations/')) return json({ conversations });
      if (path.includes('/api/v1/messages/')) return json({ messages: [], next_cursor: null });
      return json({});
    }),
  );
  const core = createNannos({}, () => new FakeSocket() as unknown as Socket);
  render(
    <NannosProvider core={core}>
      <AssistantPanel shadow={false} adapter={ADAPTER} {...props} />
    </NannosProvider>,
  );
}

const card = () => document.querySelector('[data-slot="nannos-continue-card"]');

describe('continue where you left off', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });

  // No vitest `globals`, so testing-library's auto-cleanup never runs.
  afterEach(cleanup);

  it('offers the most recent conversation over the fresh thread', async () => {
    mountPanel(CONVERSATIONS);

    const button = await screen.findByRole('button', { name: /Campaign 42 pacing/ });
    expect(button.getAttribute('data-slot')).toBe('nannos-continue-item');
    expect(card()!.textContent).toContain('Continue where you left off');
    // The newest one, not every one — this is a way back, not a second history.
    expect(card()!.textContent).not.toContain('Invoice question');
    // The row says what it is about, the way the history list does.
    expect(button.textContent).toContain('Why campaign 42 under-delivered last week.');
    // The invitation to type stays under it.
    expect(screen.getByText('How can I help?')).toBeTruthy();
  });

  it('a click reopens that conversation, and the card leaves with it', async () => {
    mountPanel(CONVERSATIONS);

    fireEvent.click(await screen.findByRole('button', { name: /Campaign 42 pacing/ }));

    const header = document.querySelector('[data-slot="nannos-panel-header"]')!;
    await waitFor(() => expect(header.textContent).toContain('Campaign 42 pacing'));
    // The user is IN that conversation now — nothing left to continue.
    await waitFor(() => expect(card()).toBeNull());

    // New chat puts the panel back on an empty thread, so the offer returns.
    fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
    await waitFor(() => expect(card()).toBeTruthy());
  });

  it('stays away in sidebar mode — the whole history is already on screen', async () => {
    mountPanel(CONVERSATIONS, { showConversationList: true });

    // The sidebar row proves the history arrived; the card still declines.
    await screen.findByText('Campaign 42 pacing');
    expect(card()).toBeNull();
  });

  it('stays away when there is no history', async () => {
    mountPanel([]);

    // The empty state is the proof the thread rendered at all.
    await screen.findByText('How can I help?');
    expect(card()).toBeNull();
  });
});
