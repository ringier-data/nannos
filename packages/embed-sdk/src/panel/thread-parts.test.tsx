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
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
function mountThread(
  messages: NannosUIMessage[],
  devMode = false,
  send: UseNannosChatValue['send'] = () => {},
) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })),
  );
  const chat = {
    messages,
    send,
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

  it('titles a client_action part with its kind, not just the tool name', () => {
    // `client_action` is four different jobs under one name — the card is
    // useless without the kind. The round-trip parts also render in dev mode
    // while still pending (the approval card never shows them).
    mountThread(
      [
        message({
          type: 'dynamic-tool',
          toolName: 'client_action',
          toolCallId: 'call-1',
          state: 'approval-requested',
          input: { directive: { kind: 'read_current_page' }, _clientActionRequest: true },
          approval: { id: 'call-1' },
        }),
      ],
      true,
    );

    expect(screen.getByText('client_action · read_current_page')).toBeTruthy();
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

describe('the authorization card', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });

  afterEach(cleanup);

  /** An `auth-required` part with a URL to send the user to. */
  const authTurn = (): NannosUIMessage => ({
    id: 'msg-1',
    role: 'assistant',
    parts: [
      {
        type: 'data-auth-required',
        id: 'auth-1',
        data: { authUrl: 'http://provider/consent', tool: 'gmail_send' },
      } as NannosUIMessage['parts'][number],
    ],
  });

  const card = () => document.querySelector('[data-slot="nannos-auth-required"]');
  const authorize = () =>
    document.querySelector('[data-slot="nannos-auth-action"]') as HTMLElement | null;
  const done = () => document.querySelector('[data-slot="nannos-auth-done"]') as HTMLElement | null;
  const reauthorize = () => document.querySelector('[data-slot="nannos-auth-retry"]');

  it('offers only the way out to the provider at first', () => {
    mountThread([authTurn()]);

    expect(authorize()).toBeTruthy();
    expect(done()).toBeNull();
    expect(reauthorize()).toBeNull();
  });

  it('swaps in the confirm and the second attempt once the window is open', () => {
    mountThread([authTurn()]);
    fireEvent.click(authorize()!);

    // The link out is now the ghost second attempt, not the primary action.
    expect(authorize()).toBeNull();
    expect(done()).toBeTruthy();
    expect(reauthorize()?.getAttribute('href')).toBe('http://provider/consent');
  });

  it('tells the agent it can retry, and takes itself off screen', () => {
    const send = vi.fn();
    mountThread([authTurn()], false, send);
    fireEvent.click(authorize()!);
    fireEvent.click(done()!);

    // Agent-facing text names the tool; the user sees the localized chip label.
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toContain('gmail_send');
    expect(send.mock.calls[0][1]).toEqual({ displayText: 'Authorization complete' });
    expect(card()).toBeNull();
  });
});

/**
 * Mid-turn notes (`notify_user`) arrive on the activity-log channel like a tool
 * label, but they are the agent SPEAKING while it works. The thread must not
 * render them as more grey machine chatter, or the one line that says "I
 * understood you" is lost among the twelve that say "Running search…".
 */
describe('mid-turn notes in the thread', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });

  afterEach(cleanup);

  const NOTE = {
    type: 'data-activity',
    id: 'act-note',
    data: { text: 'Running a health check on campaign 456.', kind: 'note', ts: TS },
  } as NannosUIMessage['parts'][number];

  const noteTurn = (): NannosUIMessage => ({
    id: 'msg-1',
    role: 'assistant',
    parts: [NOTE, ACTIVITY],
  });

  const note = () => document.querySelector('[data-slot="nannos-agent-note"]');
  const machineLine = () => document.querySelector('[data-slot="nannos-activity"]');

  it('renders a note in its own slot, not as an activity line', () => {
    mountThread([noteTurn()]);

    expect(note()?.textContent).toContain('Running a health check on campaign 456.');
    // The tool label beside it stays a machine line.
    expect(machineLine()?.textContent).toContain('Running ls…');
    expect(machineLine()?.textContent).not.toContain('campaign 456');
  });

  it('reads at answer size while a tool label stays a micro-line', () => {
    mountThread([noteTurn()]);

    expect(note()?.className).toContain('text-sm');
    expect(note()?.className).not.toContain('text-xs');
    expect(machineLine()?.className).toContain('text-xs');
  });

  it('carries the dev arrival stamp like every other agent event', () => {
    mountThread([noteTurn()], true);

    expect(note()?.textContent).toContain(CLOCK);
  });
});
