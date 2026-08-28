// @vitest-environment happy-dom
/** Copy + attachments in the thread: the readable text reaches the clipboard,
 *  and a file with a URL is a download link. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos } from '../core';
import { NannosProvider, type NannosHostAdapter } from '../react';
import type { NannosUIMessage } from '../transport';
import { Thread } from './components/thread';
import { NannosChatScope } from './engine';
import type { UseNannosChatValue } from './hooks/use-nannos-chat';

class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
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

function mountThread(messages: NannosUIMessage[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })),
  );
  const chat = {
    messages,
    send: () => {},
    isBusy: false,
    hasOlderMessages: false,
    loadOlderMessages: async () => {},
    conversationId: 'conv-1',
    interrupt: { pending: [], reason: undefined, reviewConfigs: [], respond: async () => {} },
  } as unknown as UseNannosChatValue;
  const core = createNannos({}, () => new FakeSocket() as unknown as Socket);
  render(
    <NannosProvider core={core}>
      <NannosChatScope adapter={ADAPTER}>
        <Thread chat={chat} showContinue={false} />
      </NannosChatScope>
    </NannosProvider>,
  );
}

describe('message actions', () => {
  beforeEach(() => vi.stubGlobal('ResizeObserver', FakeResizeObserver));
  afterEach(cleanup);

  it('copies the readable text of user and assistant messages', async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    mountThread([
      { id: 'u1', role: 'user', parts: [{ type: 'text', text: 'what is that?' }] },
      {
        id: 'a1',
        role: 'assistant',
        parts: [
          { type: 'data-activity', id: 'x', data: { text: 'Delegating…' } },
          { type: 'text', text: 'A gymnast.' },
        ] as NannosUIMessage['parts'],
      },
    ]);
    const buttons = screen.getAllByRole('button', { name: 'Copy' });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('what is that?'));
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('A gymnast.'));
    // The activity line is machinery, not content.
    expect(writeText).not.toHaveBeenCalledWith(expect.stringContaining('Delegating'));
  });

  it('renders file parts as download links', () => {
    mountThread([
      {
        id: 'u1',
        role: 'user',
        parts: [
          { type: 'text', text: 'see attached' },
          { type: 'file', url: 'https://s3/report.pdf?sig=1', mediaType: 'application/pdf', filename: 'report.pdf' },
        ],
      },
      {
        id: 'a1',
        role: 'assistant',
        parts: [{ type: 'file', url: 'https://s3/out.png?sig=2', mediaType: 'image/png', filename: 'out.png' }],
      },
    ]);
    const pdf = screen.getByRole('link', { name: 'Download report.pdf' });
    expect(pdf.getAttribute('href')).toBe('https://s3/report.pdf?sig=1');
    expect(pdf.getAttribute('download')).toBe('report.pdf');
    expect(screen.getByRole('link', { name: 'Download out.png' })).toBeTruthy();
    expect(screen.getByRole('img', { name: 'out.png' }).getAttribute('src')).toBe('https://s3/out.png?sig=2');
  });
});
