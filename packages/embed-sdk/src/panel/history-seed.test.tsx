// @vitest-environment happy-dom
/**
 * History seeding for the conversation the panel OPENS on. What it guards: when
 * the chat scope lives at the host's layout (the console) the list can land and
 * select a conversation before any panel mounts — that conversation must still
 * get its messages, also under React StrictMode's mount → cleanup → mount, which
 * once left it blank for good (its row was already selected, so picking it again
 * changed nothing; every other row loaded fine).
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode, useState, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos } from '../core';
import { NannosProvider, type NannosHostAdapter } from '../react';
import { AssistantPanel } from './assistant-panel';
import { NannosChatScope } from './engine';

class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** Never connects, so nothing streams (mirrors engine.test.tsx). */
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

const CONVERSATIONS = [
  {
    conversation_id: 'c1',
    title: 'First chat',
    last_message: 'x',
    last_message_at: '2026-08-25T10:00:00Z',
    status: 'active',
  },
  {
    conversation_id: 'c2',
    title: 'Second chat',
    last_message: 'x',
    last_message_at: '2026-08-24T10:00:00Z',
    status: 'active',
  },
];

const messagesOf = (id: string) => [
  { id: `u-${id}`, role: 'user', content: `hello from ${id}`, created_at: '2026-08-25T10:00:00Z' },
  {
    id: `a-${id}`,
    role: 'agent',
    kind: 'status-update',
    state: 'completed',
    content: `reply in ${id}`,
    parts: [{ kind: 'text', text: `reply in ${id}` }],
    created_at: '2026-08-25T10:00:01Z',
  },
];

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

describe('history seeding of the conversation the panel opens on', () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        const path = String(url);
        if (path.includes('/api/v1/conversations/')) return json({ conversations: CONVERSATIONS });
        const m = path.match(/\/api\/v1\/messages\/([^?]+)/);
        if (m) return json({ messages: messagesOf(m[1]), next_cursor: null });
        return json({});
      }),
    );
  });
  afterEach(cleanup);

  for (const strict of [false, true]) {
    it(`a conversation selected before the panel mounts gets its messages (StrictMode: ${strict})`, async () => {
      // The console after deleting the active chat and reloading: the session
      // record points at a replacement the server never had, so the scope falls
      // back to the most recent conversation — before any panel is on screen.
      sessionStorage.setItem('nannos-active-conversation:default', JSON.stringify({ id: 'gone' }));
      const core = createNannos({}, () => new FakeSocket() as unknown as Socket);
      let showPanel!: (on: boolean) => void;
      function Host() {
        const [on, setOn] = useState(false);
        showPanel = setOn;
        return (
          <NannosProvider core={core}>
            <NannosChatScope adapter={ADAPTER}>
              {on && (
                <AssistantPanel
                  shadow={false}
                  adapter={ADAPTER}
                  showConversationList
                  header={false}
                  layout="page"
                />
              )}
            </NannosChatScope>
          </NannosProvider>
        );
      }
      const Wrap = strict
        ? StrictMode
        : ({ children }: { children: ReactNode }) => <>{children}</>;
      render(
        <Wrap>
          <Host />
        </Wrap>,
      );
      // The list loads at layout level and selects 'c1'; only then does the
      // chat page mount its panel.
      await waitFor(() => expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(0));
      await new Promise((r) => setTimeout(r, 20));
      showPanel(true);

      await screen.findByText('reply in c1');

      // Picking the already-selected row is a no-op — and must stay non-blank.
      const list = document.querySelector('[data-slot="nannos-conversation-list"]')!;
      fireEvent.click(within(list as HTMLElement).getByText('First chat'));
      await new Promise((r) => setTimeout(r, 20));
      expect(screen.queryByText('reply in c1')).not.toBeNull();

      // The other rows load as they always did.
      fireEvent.click(within(list as HTMLElement).getByText('Second chat'));
      await screen.findByText('reply in c2');
    });
  }
});
