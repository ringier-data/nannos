// @vitest-environment happy-dom
/**
 * Engine lifecycle: React runs mount → cleanup → mount over the SAME memoized
 * engine (StrictMode in dev, Fast Refresh), so the scope's teardown has to be a
 * DETACH, not a one-way destroy. The regression it guards: the panel footer read
 * "connected" (core socket, freshly re-handshaken) while the chat transport was
 * still flagged destroyed from the cleanup → every send answered with the
 * "Not connected to the assistant backend." error stream.
 */
import { act, render } from '@testing-library/react';
import { StrictMode } from 'react';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos } from '../core';
import { NannosProvider, useAssistant, type AssistantValue, type NannosHostAdapter } from '../react';
import type { NannosUIMessage } from '../transport';
import { NannosChatScope, useChatEngine, type ChatEngine } from './engine';
import { useNannosChat, type UseNannosChatValue } from './hooks/use-nannos-chat';

/** Socket fake mirroring connection-store.test.ts: handlers by name, emits captured. */
class FakeSocket {
  connected = false;
  emitted: Array<[string, unknown]> = [];
  private handlers = new Map<string, (data: unknown) => void>();
  on(event: string, cb: (data: unknown) => void) {
    this.handlers.set(event, cb);
    return this;
  }
  emit(event: string, payload: unknown) {
    this.emitted.push([event, payload]);
  }
  connect() {
    this.connected = true;
    this.fire('connect', undefined);
  }
  disconnect() {
    this.connected = false;
    this.fire('disconnect', undefined);
  }
  fire(event: string, data: unknown) {
    this.handlers.get(event)?.(data);
  }
}

const ADAPTER: NannosHostAdapter = {
  // Skip agent-URL discovery and the persisted-settings fetch.
  defaults: { agentUrl: 'http://agent', model: 'm1' },
  api: { getUserSettings: async () => null },
};

function mountScope() {
  const sockets: FakeSocket[] = [];
  const core = createNannos({}, () => {
    const socket = new FakeSocket();
    sockets.push(socket);
    return socket as unknown as Socket;
  });
  let engine: ChatEngine | null = null;
  let assistant: AssistantValue | null = null;
  function Probe() {
    engine = useChatEngine();
    assistant = useAssistant();
    return null;
  }
  render(
    <StrictMode>
      <NannosProvider core={core}>
        <NannosChatScope adapter={ADAPTER}>
          <Probe />
        </NannosChatScope>
      </NannosProvider>
    </StrictMode>,
  );
  return {
    core,
    // The live socket: StrictMode's cleanup disconnects the first one and the
    // remount builds a second (client.disconnect() nulls it, by design).
    socket: () => sockets[sockets.length - 1]!,
    engine: () => engine!,
    assistant: () => assistant!,
  };
}

/** Complete the handshake on the live socket. */
async function handshake(socket: FakeSocket) {
  await act(async () => {
    socket.connect();
  });
  await vi.waitFor(() => expect(socket.emitted.some(([e]) => e === 'initialize_client')).toBe(true));
  await act(async () => {
    socket.fire('client_initialized', { status: 'success', agent: { name: 'Orchestrator' } });
  });
}

/** Chunks the stream has ready — an error stream is closed, a live turn idles. */
async function readAvailable(stream: ReadableStream<unknown>, ms = 30) {
  const reader = stream.getReader();
  const chunks: unknown[] = [];
  for (;;) {
    const next = await Promise.race([
      reader.read(),
      new Promise<'idle'>((resolve) => setTimeout(() => resolve('idle'), ms)),
    ]);
    if (next === 'idle' || next.done) break;
    chunks.push(next.value);
  }
  reader.releaseLock();
  return chunks as Array<{ type: string; errorText?: string }>;
}

const USER_MESSAGE: NannosUIMessage = {
  id: 'u1',
  role: 'user',
  parts: [{ type: 'text', text: 'hi' }],
};

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  // Nothing in this test should reach the network (conversation list).
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } })),
  );
});

describe('NannosChatScope lifecycle', () => {
  it('survives a StrictMode remount: the handshake lands and a send reaches the wire', async () => {
    const scope = mountScope();
    const socket = scope.socket();
    await handshake(socket);

    // The store's client subscription is live → the panel header/status see it.
    expect(scope.engine().connection.getSnapshot()).toMatchObject({ initialized: true, agentName: 'Orchestrator' });
    expect(scope.core.status).toBe('connected');

    const stream = await scope.engine().transport.sendMessages({
      trigger: 'submit-message',
      chatId: 'conv-1',
      messageId: undefined,
      messages: [USER_MESSAGE],
      abortSignal: undefined,
    });
    const chunks = await readAvailable(stream);

    expect(chunks.find((c) => c.type === 'error')).toBeUndefined();
    expect(socket.emitted.some(([e]) => e === 'send_message')).toBe(true);
  });

  it('sends read the LIVE page context: each turn carries what the host last published', async () => {
    const scope = mountScope();
    const socket = scope.socket();
    await handshake(socket);

    const sendPayloadOf = (index: number) =>
      socket.emitted.filter(([e]) => e === 'send_message')[index]?.[1] as {
        metadata?: Record<string, unknown>;
      };

    // No page context published → the key is absent entirely.
    await scope.engine().transport.sendMessages({
      trigger: 'submit-message',
      chatId: 'conv-1',
      messageId: undefined,
      messages: [USER_MESSAGE],
      abortSignal: undefined,
    });
    expect(sendPayloadOf(0).metadata && 'pageContext' in sendPayloadOf(0).metadata!).toBe(false);

    // Publish, "navigate", send again: the engine reads at send time, without
    // rebuilding (same transport instance).
    const transportBefore = scope.engine().transport;
    await act(async () => {
      scope.assistant().setPageContext({ key: '/campaigns/7', title: 'Campaign 7' });
    });
    expect(scope.engine().transport).toBe(transportBefore);
    await scope.engine().transport.sendMessages({
      trigger: 'submit-message',
      chatId: 'conv-2',
      messageId: undefined,
      messages: [USER_MESSAGE],
      abortSignal: undefined,
    });
    expect(sendPayloadOf(1).metadata?.pageContext).toEqual({
      key: '/campaigns/7',
      title: 'Campaign 7',
    });
  });

  it('handshakes a socket that connects AFTER the scope asked to initialize', async () => {
    // Child effects run before the provider's connect(), so the scope's own
    // initialize() can find no socket at all; the connect must retry it.
    const scope = mountScope();
    const socket = scope.socket();
    expect(socket.emitted.some(([e]) => e === 'initialize_client')).toBe(false);
    await handshake(socket);
    expect(scope.engine().connection.getSnapshot().initialized).toBe(true);
  });
});

describe('client-action round trip (awaited apply)', () => {
  it('auto-executes the request against the registry and resumes with the result — no approval card', async () => {
    // Full loop: turn streams → input-required client-action request arrives →
    // useNannosChat settles it (executes the directive, answers through the
    // approval machinery) → sendAutomaticallyWhen fires the resume send whose
    // decisions carry `client_action_result`.
    const sockets: FakeSocket[] = [];
    const core = createNannos({}, () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket as unknown as Socket;
    });
    core.register({
      type: 'Campaign',
      id: '7',
      scope: 'update',
      fields: ['budget', 'campaignType'],
      getState: () => ({}),
      apply: () => ({
        applied: ['budget'],
        rejected: [{ field: 'campaignType', reason: 'bad enum' }],
      }),
    });

    let chatValue: UseNannosChatValue | null = null;
    function Probe() {
      chatValue = useNannosChat('conv-ca');
      return null;
    }
    render(
      <StrictMode>
        <NannosProvider core={core}>
          <NannosChatScope adapter={ADAPTER}>
            <Probe />
          </NannosChatScope>
        </NannosProvider>
      </StrictMode>,
    );
    const socket = sockets[sockets.length - 1]!;
    await handshake(socket);

    // The user asks; the turn opens on the wire.
    await act(async () => {
      chatValue!.send('set the budget to 50k');
    });
    await vi.waitFor(() =>
      expect(socket.emitted.filter(([e]) => e === 'send_message')).toHaveLength(1),
    );

    // The paused tool's request arrives as input-required + client-action ext.
    await act(async () => {
      socket.fire('agent_response', {
        kind: 'status-update',
        contextId: 'conv-ca',
        status: {
          state: 'input-required',
          message: {
            extensions: ['urn:nannos:a2a:client-action:1.0'],
            parts: [
              {
                kind: 'data',
                data: {
                  request: {
                    id: 'call-9',
                    directive: {
                      kind: 'apply',
                      target: { type: 'Campaign', id: '7' },
                      values: { budget: 50000, campaignType: 'Sponsoring' },
                    },
                  },
                },
              },
            ],
          },
        },
      });
    });

    // The resume goes out by itself, carrying the browser's actual result.
    await vi.waitFor(() => {
      expect(socket.emitted.filter(([e]) => e === 'send_message')).toHaveLength(2);
    });
    const resume = socket.emitted.filter(([e]) => e === 'send_message')[1][1] as {
      message: string;
      dataParts?: Array<{ decisions?: Array<Record<string, unknown>> }>;
    };
    expect(resume.message).toBe('');
    const decision = resume.dataParts?.[0]?.decisions?.[0];
    expect(decision).toMatchObject({
      id: 'call-9',
      type: 'approve',
      client_action_result: {
        ok: true,
        applied: ['budget'],
        rejected: [{ field: 'campaignType', reason: 'bad enum' }],
      },
    });

    // Machine-answered: at no point was a human approval surfaced.
    expect(chatValue!.interrupt.pending).toHaveLength(0);
  });
});
