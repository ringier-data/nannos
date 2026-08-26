// @vitest-environment happy-dom
/**
 * What the thread does with an assistant message's parts.
 *
 * Tool parts: HITL parts are the only ones the panel ever gets, and none of
 * them belong in the end-user view — a pending one is the `<ApprovalCard>`'s
 * job, and an answered one settles to a synthetic `{approved: true}` output, a
 * box with no readable result that a reload then drops anyway. So an approved
 * tool reads exactly like a tool that never needed approval (its activity
 * lines, then the answer), while dev mode keeps the raw part for inspection.
 *
 * Dev timestamps: every agent event carries its arrival time — activity lines
 * in `data.ts`, the answer in its provider metadata — so dev mode reads the
 * turn as one timeline, and the end-user view shows none of it.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos } from '../core';
import { NannosProvider, type NannosHostAdapter } from '../react';
import { textArrival, type NannosUIMessage } from '../transport';
import { Thread } from './components/thread';
import { DevModeProvider } from './dev-mode';
import { NannosChatScope } from './engine';
import type { UseNannosChatValue } from './hooks/use-nannos-chat';

/** The thread's stick-to-bottom needs it. */
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** Never connects, so nothing streams into the hand-built message list. */
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

/** 16:11:32 local — the time the two stamps in these tests are read back at. */
const TS = new Date(2026, 7, 26, 16, 11, 32).getTime();
const CLOCK = new Date(TS).toLocaleTimeString(undefined, { hour12: false });

const ACTIVITY = {
  type: 'data-activity',
  id: 'act-1',
  data: { text: 'Running ls…', ts: TS },
} as NannosUIMessage['parts'][number];

const TOOL_INPUT = { path: '/memories/', _call_id: 'call-1' };

function message(toolPart: Record<string, unknown>): NannosUIMessage {
  return {
    id: 'msg-1',
    role: 'assistant',
    parts: [ACTIVITY, toolPart as NannosUIMessage['parts'][number]],
  };
}

/** An answered turn: the activity line, then the stamped answer. */
function answeredTurn(stamped = true): NannosUIMessage {
  return {
    id: 'msg-1',
    role: 'assistant',
    parts: [
      ACTIVITY,
      {
        type: 'text',
        text: 'Here is what I found.',
        ...(stamped && { providerMetadata: textArrival(TS) }),
      } as NannosUIMessage['parts'][number],
    ],
  };
}

/** Thread renders from its `chat` prop; the scope only satisfies context. */
function mountThread(messages: NannosUIMessage[], devMode = false) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })),
  );
  const chat = {
    messages,
    isBusy: false,
    hasOlderMessages: false,
    loadOlderMessages: async () => {},
    conversationId: 'conv-1',
  } as unknown as UseNannosChatValue;
  const core = createNannos({}, () => new FakeSocket() as unknown as Socket);
  render(
    <NannosProvider core={core}>
      <NannosChatScope adapter={ADAPTER}>
        <DevModeProvider enabled={devMode}>
          <Thread chat={chat} showContinue={false} />
        </DevModeProvider>
      </NannosChatScope>
    </NannosProvider>,
  );
}

const toolBox = () => document.querySelector('[data-slot="nannos-tool"]');

describe('tool parts in the thread', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });

  // No vitest `globals`, so testing-library's auto-cleanup never runs.
  afterEach(cleanup);

  it('leaves no box behind once the tool is approved', () => {
    mountThread([
      message({
        type: 'dynamic-tool',
        toolName: 'ls',
        toolCallId: 'call-1',
        state: 'output-available',
        input: TOOL_INPUT,
        output: { approved: true },
      }),
    ]);

    // The activity line is the proof the message rendered at all — and it is
    // the whole story the user gets, exactly as for an unapproved tool.
    expect(screen.getByText('Running ls…')).toBeTruthy();
    expect(toolBox()).toBeNull();
    expect(screen.queryByText('Parameters')).toBeNull();
    expect(screen.queryByText('Result')).toBeNull();
  });

  it('leaves no box behind when the tool is denied either', () => {
    mountThread([
      message({
        type: 'dynamic-tool',
        toolName: 'ls',
        toolCallId: 'call-1',
        state: 'output-denied',
        input: TOOL_INPUT,
      }),
    ]);

    expect(toolBox()).toBeNull();
  });

  it('keeps a pending approval out of the thread — the card owns it', () => {
    mountThread([
      message({
        type: 'dynamic-tool',
        toolName: 'ls',
        toolCallId: 'call-1',
        state: 'approval-requested',
        input: TOOL_INPUT,
        approval: { id: 'call-1' },
      }),
    ]);

    expect(toolBox()).toBeNull();
  });

  it('shows the raw part in dev mode, framed as dev only', () => {
    mountThread(
      [
        message({
          type: 'dynamic-tool',
          toolName: 'ls',
          toolCallId: 'call-1',
          state: 'output-available',
          input: TOOL_INPUT,
          output: { approved: true },
        }),
      ],
      true,
    );

    expect(toolBox()).toBeTruthy();
    expect(screen.getByText('dev only')).toBeTruthy();
  });
});

describe('dev timestamps', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });

  afterEach(cleanup);

  it('times the answer, not just the activity line, in dev mode', () => {
    mountThread([answeredTurn()], true);

    // Both events of the turn are on the same clock: the tool call and the
    // answer it produced.
    expect(screen.getAllByText(CLOCK)).toHaveLength(2);
  });

  it('shows no clock at all in the end-user view', () => {
    mountThread([answeredTurn()]);

    expect(screen.getByText('Here is what I found.')).toBeTruthy();
    expect(screen.queryByText(CLOCK)).toBeNull();
  });

  it('leaves an unstamped answer (older history) alone', () => {
    mountThread([answeredTurn(false)], true);

    // The activity line still has its own stamp; the answer simply has none.
    expect(screen.getAllByText(CLOCK)).toHaveLength(1);
  });
});
