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
import { Thread, mergeAssistantRuns } from './components/thread';
import { DevModeProvider } from './dev-mode';
import { PanelLayoutProvider, type PanelLayout } from './layout';
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
  { layout = 'panel', isBusy = false }: { layout?: PanelLayout; isBusy?: boolean } = {},
) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })),
  );
  const chat = {
    messages,
    send,
    isBusy,
    hasOlderMessages: false,
    loadOlderMessages: async () => {},
    conversationId: 'conv-1',
  } as unknown as UseNannosChatValue;
  const core = createNannos({}, () => new FakeSocket() as unknown as Socket);
  render(
    <NannosProvider core={core}>
      <NannosChatScope adapter={ADAPTER}>
        <DevModeProvider enabled={devMode}>
          <PanelLayoutProvider layout={layout}>
            <Thread chat={chat} showContinue={false} />
          </PanelLayoutProvider>
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

/**
 * Page layout folds a turn's machine lines into one disclosure: at full width
 * a loose grey stream of "Running X…" is the most prominent thing on screen,
 * the opposite of what those lines are for. Notes and answers between two runs
 * keep their place; the panel layout and dev mode keep the flat stream.
 */
describe('activity folding in page layout', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });

  afterEach(cleanup);

  const line = (id: string, text: string) =>
    ({ type: 'data-activity', id, data: { text, ts: TS } }) as NannosUIMessage['parts'][number];
  const NOTE = {
    type: 'data-activity',
    id: 'act-note',
    data: { text: 'Understood, checking.', kind: 'note', ts: TS },
  } as NannosUIMessage['parts'][number];
  const turn = (): NannosUIMessage => ({
    id: 'msg-1',
    role: 'assistant',
    parts: [
      line('a1', 'Agent execution started.'),
      line('a2', 'Running github_get_me…'),
      NOTE,
      line('a3', 'Running ls…'),
      { type: 'text', text: 'Done.' },
    ],
  });

  const groups = () => document.querySelectorAll('[data-slot="nannos-activity-group"]');
  const lines = () => document.querySelectorAll('[data-slot="nannos-activity"]');

  it('keeps the flat stream in panel layout', () => {
    mountThread([turn()]);
    expect(groups().length).toBe(0);
    expect(lines().length).toBe(3);
  });

  it('folds each run of machine lines, leaving the note in the timeline', () => {
    mountThread([turn()], false, () => {}, { layout: 'page' });
    expect(groups().length).toBe(2);
    expect(lines().length).toBe(0);
    expect(screen.getByText('Worked through 2 steps')).toBeTruthy();
    expect(screen.getByText('Worked through 1 step')).toBeTruthy();
    expect(document.querySelector('[data-slot="nannos-agent-note"]')?.textContent).toContain(
      'Understood, checking.',
    );
  });

  it('opens a folded group on click', () => {
    mountThread([turn()], false, () => {}, { layout: 'page' });
    fireEvent.click(screen.getByText('Worked through 2 steps'));
    expect(lines().length).toBe(2);
    expect(screen.getByText('Running github_get_me…')).toBeTruthy();
  });

  it('stays open while the turn is in progress, showing the latest line', () => {
    mountThread([turn()], false, () => {}, { layout: 'page', isBusy: true });
    expect(lines().length).toBe(3);
    expect(screen.getAllByText('Running ls…').length).toBeGreaterThan(0);
  });

  it('keeps the flat stream in dev mode even on a page', () => {
    mountThread([turn()], true, () => {}, { layout: 'page' });
    expect(groups().length).toBe(0);
    expect(lines().length).toBe(3);
  });
});

/**
 * A thought that is the answer verbatim (the model replied in plain text, the
 * orchestrator routed it to the thinking channel, the fallback surfaced it as
 * the reply) is a duplicate, not reasoning — the end-user view drops it, dev
 * mode keeps it for inspection.
 */
describe('echoed orchestrator thoughts', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });

  afterEach(cleanup);

  const thought = (text: string, complete = true) =>
    ({
      type: 'data-agent-thought',
      id: 'th-1',
      data: { agent: 'orchestrator', text, complete, startedAt: TS },
    }) as NannosUIMessage['parts'][number];
  const turn = (text: string, complete?: boolean): NannosUIMessage => ({
    id: 'msg-1',
    role: 'assistant',
    parts: [thought(text, complete), { type: 'text', text: 'The image shows a 2x2 grid.\n' }],
  });
  const block = () => document.querySelector('[data-slot="nannos-agent-thought"]');

  it('drops a completed thought equal to the answer', () => {
    mountThread([turn('The image shows a 2x2 grid.')]);
    expect(block()).toBeNull();
  });

  it('keeps a thought that differs from the answer', () => {
    mountThread([turn('Let me look at the picture first.')]);
    expect(block()).not.toBeNull();
  });

  it('keeps a still-streaming thought — the answer may not exist yet', () => {
    mountThread([turn('The image shows a 2x2 grid.', false)]);
    expect(block()).not.toBeNull();
  });

  it('keeps the echo in dev mode', () => {
    mountThread([turn('The image shows a 2x2 grid.')], true);
    expect(block()).not.toBeNull();
  });
});

/**
 * A HITL pause splits one turn into two assistant messages on the live path;
 * the thread renders them as one block, and the decision itself leaves a line
 * where the work broke off — so the reader sees "approved X" between the steps
 * before and after, not two answers with a feedback row in the middle.
 */
describe('a turn interrupted by an approval', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });

  afterEach(cleanup);

  const approved = {
    type: 'dynamic-tool',
    toolCallId: 'call-1',
    toolName: 'github_get_me',
    state: 'output-available',
    input: {},
    output: { approved: true },
  } as NannosUIMessage['parts'][number];
  const before: NannosUIMessage = {
    id: 'msg-1',
    role: 'assistant',
    parts: [ACTIVITY, approved],
  };
  const after: NannosUIMessage = {
    id: 'msg-2',
    role: 'assistant',
    parts: [ACTIVITY, { type: 'text', text: 'You are aartaria.' }],
    metadata: { persistedMessageId: 'srv-2' },
  };

  it('merges consecutive assistant messages, keeping the last identity', () => {
    const user: NannosUIMessage = { id: 'u', role: 'user', parts: [{ type: 'text', text: 'hi' }] };
    const merged = mergeAssistantRuns([user, before, after]);
    expect(merged.length).toBe(2);
    expect(merged[1].id).toBe('msg-2');
    expect(merged[1].parts.length).toBe(4);
    expect(merged[1].metadata?.persistedMessageId).toBe('srv-2');
  });

  it('renders one block with one feedback row', () => {
    mountThread([before, after]);
    expect(document.querySelectorAll('[data-slot="nannos-message-actions"]').length).toBe(1);
  });

  it('acknowledges the approved call as a machine line', () => {
    mountThread([before, after]);
    expect(screen.getByText('Approved github_get_me')).toBeTruthy();
    expect(toolBox()).toBeNull();
  });

  it('folds the acknowledgement into the step group on a page', () => {
    mountThread([before, after], false, () => {}, { layout: 'page' });
    // Two labels + the decision = one run of three machine lines.
    expect(screen.getByText('Worked through 3 steps')).toBeTruthy();
  });
});
