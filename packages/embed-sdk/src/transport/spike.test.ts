/**
 * Phase-0 spike gates (S1–S5): does the pinned AI SDK v7 accept our
 * A2A-over-socket streams end to end? Runs the REAL `Chat` state machine from
 * `@ai-sdk/react` (headless — its React layer is only change-callbacks)
 * against `A2AChatTransport` over a scripted fake wire that mirrors
 * TransportClient's delivery semantics (incl. the pendingHitl snapshot
 * replay, client.ts:168-177).
 *
 * Fallback triggers (plan): F1 mid-stream setMessages corrupts streaming,
 * F2 sendAutomaticallyWhen mis-fires on batches, F4 dynamic-tool approval
 * defects. Each has a test here; a red one fires the documented Plan B.
 */
import { describe, expect, it, vi } from 'vitest';
import { Chat } from '@ai-sdk/react';
import { A2AChatTransport } from './a2a-transport';
import {
  lastAssistantMessageIsCompleteWithApprovalResponses,
  textArrivalTs,
  type NannosUIMessage,
} from './ai-types';
import { HITL_EXT, INTERMEDIATE_OUTPUT_EXT, WORK_PLAN_EXT, ACTIVITY_LOG_EXT } from '../core/extensions';
import type { AgentResponseData, ConversationSnapshotData, SendMessagePayload } from '../core/wire';
import type { TransportState } from '../core/client';

class FakeWire {
  sent: SendMessagePayload[] = [];
  cancelled: string[] = [];
  subscribed: string[] = [];
  private responseListeners = new Set<(d: AgentResponseData) => void>();
  private snapshotListeners = new Set<(d: ConversationSnapshotData) => void>();
  private stateListeners = new Set<(s: TransportState) => void>();

  onAgentResponse(cb: (d: AgentResponseData) => void) {
    this.responseListeners.add(cb);
    return () => this.responseListeners.delete(cb);
  }
  onConversationSnapshot(cb: (d: ConversationSnapshotData) => void) {
    this.snapshotListeners.add(cb);
    return () => this.snapshotListeners.delete(cb);
  }
  subscribe(cb: (s: TransportState) => void) {
    this.stateListeners.add(cb);
    return () => this.stateListeners.delete(cb);
  }
  sendMessage(payload: SendMessagePayload) {
    this.sent.push(payload);
    return true;
  }
  cancelTask(conversationId: string) {
    this.cancelled.push(conversationId);
    return true;
  }
  /** Answers the subscribe ack the way the backend does, when armed. */
  subscribeAck: ((conversationId: string) => { ok: boolean; inFlight: boolean } | null) | null = null;
  subscribeConversation(
    conversationId: string,
    ack?: (result: { ok: boolean; inFlight: boolean } | null) => void,
  ) {
    this.subscribed.push(conversationId);
    if (ack && this.subscribeAck) ack(this.subscribeAck(conversationId));
    return true;
  }

  emit(data: AgentResponseData) {
    for (const l of this.responseListeners) l(data);
  }
  /** Mirrors TransportClient: pendingHitl replays through agent_response FIRST. */
  emitSnapshot(snap: ConversationSnapshotData) {
    if (snap.pendingHitl) for (const l of this.responseListeners) l(snap.pendingHitl);
    for (const l of this.snapshotListeners) l(snap);
  }
  reconnect() {
    for (const l of this.stateListeners) l({ socketConnected: true, initialized: true, agentInfo: null });
  }
}

const CONV = 'conv-1';

function setup(opts?: { sendAutomatically?: boolean }) {
  const wire = new FakeWire();
  const turnEvents: string[] = [];
  const transport = new A2AChatTransport({
    client: wire,
    whenReady: async () => true,
    getSendContext: () => ({ sessionId: 'sess-1', executeOnlySubAgentId: 42, clientObjects: [{ type: 'Invoice', id: '7', scope: 'update', fields: ['amount'] }] }),
    snapshotTimeoutMs: 200,
    onTurnEvent: (e) => turnEvents.push(e.type),
  });
  const chat = new Chat<NannosUIMessage>({
    id: CONV,
    transport,
    messages: [],
    ...(opts?.sendAutomatically && {
      sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithApprovalResponses,
    }),
  });
  return { wire, transport, chat, turnEvents };
}

function streamChunk(text: string, turnOffset?: number): AgentResponseData {
  return {
    contextId: CONV,
    kind: 'artifact-update',
    artifact: { parts: [{ kind: 'text', text }] },
    ...(turnOffset !== undefined && { turnOffset }),
  };
}

function terminal(state = 'completed'): AgentResponseData {
  return { contextId: CONV, kind: 'status-update', role: 'agent', status: { state } };
}

function hitlInterrupt(actions: Array<{ name: string; callId: string }>): AgentResponseData {
  return {
    contextId: CONV,
    kind: 'status-update',
    role: 'agent',
    status: {
      state: 'input-required',
      message: {
        extensions: [HITL_EXT],
        parts: [
          { kind: 'text', text: 'Please review before I proceed.' },
          {
            kind: 'data',
            data: {
              action_requests: actions.map((a) => ({
                name: a.name,
                args: { _call_id: a.callId, city: 'Zürich', _risk_metadata: { source: 'risk_score', score: 0.85 } },
              })),
              review_configs: actions.map((a) => ({
                action_name: a.name,
                allowed_decisions: ['approve', 'reject'],
              })),
            },
          },
        ],
      },
    },
  } as AgentResponseData;
}

const lastAssistant = (chat: Chat<NannosUIMessage>) =>
  [...chat.messages].reverse().find((m) => m.role === 'assistant');

const textOf = (m: NannosUIMessage | undefined) =>
  (m?.parts ?? [])
    .filter((p): p is Extract<(typeof m extends undefined ? never : NannosUIMessage)['parts'][number], { type: 'text' }> => p.type === 'text')
    .map((p) => p.text)
    .join('');

describe('S1 — basic turn', () => {
  it('streams progressive text with interleaved data parts and finalizes on terminal', async () => {
    const { wire, chat } = setup();
    void chat.sendMessage({ text: 'hello' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));

    expect(wire.sent[0].message).toBe('hello');
    expect(wire.sent[0].metadata?.executeOnlySubAgentId).toBe(42);
    expect(wire.sent[0].metadata?.clientObjects).toHaveLength(1);

    wire.emit({
      contextId: CONV,
      kind: 'status-update',
      status: { message: { extensions: [ACTIVITY_LOG_EXT], parts: [{ kind: 'text', text: 'Calling tool…' }], metadata: { source: 'billing-agent' } } },
    } as AgentResponseData);
    wire.emit({
      contextId: CONV,
      kind: 'status-update',
      status: { message: { extensions: [WORK_PLAN_EXT], parts: [{ kind: 'data', data: { todos: [{ name: 'step 1', state: 'working' }] } }] } },
    } as AgentResponseData);
    wire.emit(streamChunk('Hello ', 6));
    wire.emit(streamChunk('world', 11));
    wire.emit({ ...terminal(), persistedMessageId: 'db-123' });

    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    const msg = lastAssistant(chat)!;
    expect(textOf(msg)).toBe('Hello world');
    expect(msg.parts.some((p) => p.type === 'data-activity')).toBe(true);
    expect(msg.parts.some((p) => p.type === 'data-workplan')).toBe(true);

    // The answer carries its arrival time, so dev mode can time it the way it
    // times the activity line above it — and never earlier than that line.
    const activityTs = (msg.parts.find((p) => p.type === 'data-activity') as { data: { ts: number } })
      .data.ts;
    const answerTs = textArrivalTs(msg.parts.find((p) => p.type === 'text')!);
    expect(answerTs).toBeTypeOf('number');
    expect(answerTs!).toBeGreaterThanOrEqual(activityTs);
  });

  it('sub-agent thoughts reconcile in place and close when streaming starts', async () => {
    const { wire, chat } = setup();
    void chat.sendMessage({ text: 'go' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));

    const thought = (text: string): AgentResponseData => ({
      contextId: CONV,
      kind: 'artifact-update',
      artifact: { parts: [{ kind: 'text', text }], extensions: [INTERMEDIATE_OUTPUT_EXT], metadata: { agent_name: 'researcher' } },
    } as AgentResponseData);
    wire.emit(thought('thinking'));
    wire.emit(thought(' hard'));
    wire.emit(streamChunk('Answer', 6));
    wire.emit(terminal());

    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    const msg = lastAssistant(chat)!;
    const thoughts = msg.parts.filter((p) => p.type === 'data-agent-thought');
    expect(thoughts).toHaveLength(1); // id-reconciled, not appended
    expect((thoughts[0] as { data: { text: string; complete: boolean } }).data).toMatchObject({
      text: 'thinking hard',
      complete: true,
    });
    expect(textOf(msg)).toBe('Answer');
  });
});

describe('S2 — code-point offsets (the emoji bug)', () => {
  it('dedupes overlapping chunks and snapshot seeds by code points, not UTF-16 units', async () => {
    const { wire, chat } = setup();
    void chat.sendMessage({ text: 'emoji' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));

    wire.emit(streamChunk('👋👋', 2)); // 2 code points, 4 UTF-16 units
    wire.emit(streamChunk('👋👋', 2)); // duplicate delivery → must drop
    // Reconnect-style snapshot covering the head plus new tail:
    wire.emitSnapshot({ conversationId: CONV, inFlight: true, offset: 4, replyText: '👋👋🌍!' });
    wire.emit(streamChunk('🌍!', 4)); // already covered by snapshot → must drop
    wire.emit(streamChunk(' done', 9));
    wire.emit(terminal());

    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    expect(textOf(lastAssistant(chat))).toBe('👋👋🌍! done');
  });
});

describe('S3 — HITL round-trip (native tool approval)', () => {
  it('single action: approve → exactly one resume send with the decision payload', async () => {
    const { wire, chat } = setup({ sendAutomatically: true });
    void chat.sendMessage({ text: 'book it' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));

    wire.emit(streamChunk('Let me confirm first.', 21));
    wire.emit(hitlInterrupt([{ name: 'book_flight', callId: 'call-1' }]));

    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    const msg = lastAssistant(chat)!;
    const tool = msg.parts.find((p) => p.type === 'dynamic-tool') as {
      state: string;
      toolName: string;
      approval: { id: string };
      input: Record<string, unknown>;
    };
    expect(tool).toBeDefined();
    expect(tool.state).toBe('approval-requested');
    expect(tool.toolName).toBe('book_flight');
    expect(tool.input._risk_metadata).toMatchObject({ score: 0.85 });
    expect(msg.metadata?.hitl?.reviewConfigs).toHaveLength(1);

    await chat.addToolApprovalResponse({ id: tool.approval.id, approved: true });

    await vi.waitFor(() => expect(wire.sent).toHaveLength(2));
    const resume = wire.sent[1];
    expect(resume.message).toBe('');
    expect(resume.dataParts).toEqual([{ decisions: [{ id: 'call-1', type: 'approve' }] }]);
    expect(resume.metadata?.executeOnlySubAgentId).toBe(42); // re-attached (ADR-0004)
    expect(resume.metadata?.clientObjects).toHaveLength(1);

    wire.emit(streamChunk('Booked!', 7));
    wire.emit(terminal());
    await vi.waitFor(() => expect(chat.status).toBe('ready'));

    // The resume CONTINUES the interrupted assistant message: one assistant
    // message total, holding the settled tool part AND the resumed answer.
    const assistants = chat.messages.filter((m) => m.role === 'assistant');
    expect(assistants).toHaveLength(1);
    const settled = assistants[0].parts.find((p) => p.type === 'dynamic-tool') as { state: string };
    expect(settled.state).toBe('output-available');
    expect(textOf(assistants[0])).toContain('Booked!');

    // No sendAutomaticallyWhen re-fire after settling:
    await new Promise((r) => setTimeout(r, 50));
    expect(wire.sent).toHaveLength(2);
  });

  it('a pendingHitl snapshot racing the resume neither duplicates the card nor kills the turn', async () => {
    // The reproduction (from a real wire log): the SDK used to subscribe right
    // after every send, and the backend answers a subscribe with a snapshot
    // that STILL holds the prompt being answered — its
    // `_pending_interactions.pop` sits behind the ownership check and the DB
    // write. The replayed prompt re-asked `call-1` (a second approval card for
    // one call) and its `input-required` closed the resumed stream, so the
    // answer routed nowhere and the ghost card's approval failed the task.
    const { wire, chat } = setup({ sendAutomatically: true });
    void chat.sendMessage({ text: 'read your memory' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    // Fix 1: a send never subscribes — the backend joins the room itself.
    expect(wire.subscribed).toEqual([]);

    wire.emit(hitlInterrupt([{ name: 'ls', callId: 'call-1' }]));
    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    await chat.addToolApprovalResponse({ id: 'call-1', approved: true });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(2));

    // Fix 2: however the stale prompt arrives (a raced subscribe, a socket
    // rejoin, another tab), a re-ask of an answered call is dropped.
    wire.emitSnapshot({
      conversationId: CONV,
      inFlight: false,
      offset: 0,
      replyText: '',
      pendingHitl: hitlInterrupt([{ name: 'ls', callId: 'call-1' }]),
    });

    // The turn is still open, so the resumed answer still lands.
    wire.emit(streamChunk('Three files.', 12));
    wire.emit(terminal());
    await vi.waitFor(() => expect(chat.status).toBe('ready'));

    const assistants = chat.messages.filter((m) => m.role === 'assistant');
    expect(assistants).toHaveLength(1);
    const tools = assistants[0].parts.filter((p) => p.type === 'dynamic-tool') as Array<{
      state: string;
    }>;
    expect(tools).toHaveLength(1);
    expect(tools[0].state).toBe('output-available');
    expect(textOf(assistants[0])).toContain('Three files.');
    expect(wire.sent).toHaveLength(2); // no second resume
  });

  it('the dedupe is per call: a prompt for a NEW call in the resumed turn renders', async () => {
    const { wire, chat } = setup({ sendAutomatically: true });
    void chat.sendMessage({ text: 'read your memory' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    wire.emit(hitlInterrupt([{ name: 'ls', callId: 'call-1' }]));
    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    await chat.addToolApprovalResponse({ id: 'call-1', approved: true });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(2));

    // The resumed turn stops at the next risky tool — a different `_call_id`.
    wire.emit(hitlInterrupt([{ name: 'read_file', callId: 'call-2' }]));
    await vi.waitFor(() => {
      const tools = lastAssistant(chat)!.parts.filter((p) => p.type === 'dynamic-tool') as Array<{
        state: string;
      }>;
      expect(tools).toHaveLength(2);
      expect(tools[1].state).toBe('approval-requested');
    });
  });

  it('rejection with message rides the versioned reason envelope', async () => {
    const { wire, chat } = setup({ sendAutomatically: true });
    void chat.sendMessage({ text: 'delete everything' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    wire.emit(hitlInterrupt([{ name: 'delete_all', callId: 'call-9' }]));
    await vi.waitFor(() => expect(chat.status).toBe('ready'));

    await chat.addToolApprovalResponse({
      id: 'call-9',
      approved: false,
      reason: JSON.stringify({ v: 1, type: 'edit', message: 'only the drafts' }),
    });

    await vi.waitFor(() => expect(wire.sent).toHaveLength(2));
    expect(wire.sent[1].dataParts).toEqual([
      { decisions: [{ id: 'call-9', type: 'edit', message: 'only the drafts' }] },
    ]);
  });

  it('F2 gate — batch of two actions: two approvals, exactly ONE resume send with both decisions', async () => {
    const { wire, chat } = setup({ sendAutomatically: true });
    void chat.sendMessage({ text: 'do both' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    wire.emit(hitlInterrupt([
      { name: 'update_campaign', callId: 'call-a' },
      { name: 'notify_sales', callId: 'call-b' },
    ]));
    await vi.waitFor(() => expect(chat.status).toBe('ready'));

    const parts = lastAssistant(chat)!.parts.filter((p) => p.type === 'dynamic-tool');
    expect(parts).toHaveLength(2);

    await chat.addToolApprovalResponse({ id: 'call-a', approved: true });
    // Must NOT have fired yet — one approval is still pending.
    await new Promise((r) => setTimeout(r, 25));
    expect(wire.sent).toHaveLength(1);

    await chat.addToolApprovalResponse({ id: 'call-b', approved: false });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(2));
    expect(wire.sent).toHaveLength(2); // exactly one resume
    expect(wire.sent[1].dataParts).toEqual([
      { decisions: [{ id: 'call-a', type: 'approve' }, { id: 'call-b', type: 'reject' }] },
    ]);
  });
});

describe('S4 — reconnect', () => {
  it('resumeStream seeds from an in-flight snapshot and continues live', async () => {
    const { wire, chat } = setup();
    // resumeStream resolves only when the resumed turn finishes — feed the
    // tail while it is pending, then await it.
    const resume = chat.resumeStream();
    await vi.waitFor(() => expect(wire.subscribed).toContain(CONV));
    wire.emitSnapshot({ conversationId: CONV, inFlight: true, offset: 5, replyText: 'Hello' });
    await vi.waitFor(() => expect(textOf(lastAssistant(chat))).toBe('Hello'));

    wire.emit(streamChunk(' again', 11));
    wire.emit(terminal());
    await resume;
    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    expect(textOf(lastAssistant(chat))).toBe('Hello again');
  });

  it('reload while HITL pending: pendingHitl replay restores the approval card and the round-trip works', async () => {
    const { wire, chat } = setup({ sendAutomatically: true });
    const resume = chat.resumeStream();
    await vi.waitFor(() => expect(wire.subscribed).toContain(CONV));
    wire.emitSnapshot({
      conversationId: CONV,
      inFlight: false,
      offset: 0,
      replyText: '',
      pendingHitl: hitlInterrupt([{ name: 'book_flight', callId: 'call-r' }]),
    });
    await resume;

    await vi.waitFor(() => {
      const tool = lastAssistant(chat)?.parts.find((p) => p.type === 'dynamic-tool');
      expect(tool).toBeDefined();
      expect((tool as { state: string }).state).toBe('approval-requested');
    });

    await chat.addToolApprovalResponse({ id: 'call-r', approved: true });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    expect(wire.sent[0].dataParts).toEqual([{ decisions: [{ id: 'call-r', type: 'approve' }] }]);
  });

  it('a pending resume probe is NOT an active turn: the first send opens a real turn, never a steer', async () => {
    // The reproduction: a just-created conversation is unknown to the backend,
    // so subscribe_conversation is rejected and NO snapshot ever arrives — the
    // probe stays open for its whole timeout. A send inside that window used to
    // be classified as steering, so the reply routed nowhere.
    const { wire, transport, chat } = setup();
    const resume = chat.resumeStream();
    await vi.waitFor(() => expect(wire.subscribed).toContain(CONV));
    expect(transport.hasActiveTurn(CONV)).toBe(false);
    expect(transport.steer(CONV, 'would be lost')).toBeNull();
    expect(wire.sent).toHaveLength(0);

    void chat.sendMessage({ text: 'first message' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    expect(transport.hasActiveTurn(CONV)).toBe(true);

    // The send's own stream owns the conversation: the reply renders, and the
    // abandoned probe never attaches an empty assistant message.
    wire.emit(streamChunk('Hi there', 8));
    wire.emit(terminal());
    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    await resume;
    expect(textOf(lastAssistant(chat))).toBe('Hi there');
    expect(chat.messages.filter((m) => m.role === 'assistant')).toHaveLength(1);
  });

  it('a CONFIRMED in-flight resume is steerable again', async () => {
    const { wire, transport, chat } = setup();
    const resume = chat.resumeStream();
    await vi.waitFor(() => expect(wire.subscribed).toContain(CONV));
    wire.emitSnapshot({ conversationId: CONV, inFlight: true, offset: 5, replyText: 'Hello' });
    await vi.waitFor(() => expect(transport.hasActiveTurn(CONV)).toBe(true));
    expect(transport.steer(CONV, 'and Basel')).not.toBeNull();
    wire.emit(terminal());
    await resume;
  });

  it('a rejected subscribe ends the resume at once — no snapshot, no timeout wait', async () => {
    // What a just-created conversation does: the backend has never persisted
    // it, so it rejects the room join and emits no snapshot ever.
    const { wire, transport, chat, turnEvents } = setup();
    wire.subscribeAck = () => ({ ok: false, inFlight: false });

    const started = Date.now();
    await chat.resumeStream();
    // The 200ms snapshot budget of this harness was never spent.
    expect(Date.now() - started).toBeLessThan(150);
    expect(transport.hasActiveTurn(CONV)).toBe(false);
    expect(chat.messages).toHaveLength(0);
    expect(chat.status).toBe('ready');
    // Nothing to reconcile: the backend does not know this conversation.
    expect(turnEvents).toEqual([]);
  });

  it('an idle-but-known conversation ends the resume at once and reconciles the list', async () => {
    const { wire, chat, turnEvents } = setup();
    wire.subscribeAck = () => ({ ok: true, inFlight: false });

    const started = Date.now();
    await chat.resumeStream();
    expect(Date.now() - started).toBeLessThan(150);
    expect(chat.status).toBe('ready');
    // The turn may have finished while we were away — refresh the previews.
    expect(turnEvents).toEqual(['reconcile']);
  });

  it('no active turn: snapshot !inFlight → resume resolves with nothing rendered', async () => {
    const { wire, chat } = setup();
    const resume = chat.resumeStream();
    await vi.waitFor(() => expect(wire.subscribed).toContain(CONV));
    wire.emitSnapshot({ conversationId: CONV, inFlight: false, offset: 0, replyText: '' });
    await resume;
    expect(chat.messages).toHaveLength(0);
    expect(chat.status).toBe('ready');
  });
});

describe('S5 — steering (F1 gate: mid-stream setMessages)', () => {
  it('a steered send leaves the original stream unbroken and the user message visible', async () => {
    const { wire, transport, chat } = setup();
    void chat.sendMessage({ text: 'start' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    wire.emit(streamChunk('Working on', 10));
    await vi.waitFor(() => expect(textOf(lastAssistant(chat))).toBe('Working on'));

    // Host facade path: never chat.sendMessage while a turn is active. The
    // steered user bubble is inserted BEFORE the streaming assistant message
    // (matching today's UI, where the stream renders below), which also keeps
    // the streaming message LAST — the AI SDK continues it by replace-last.
    const steered = transport.steer(CONV, 'also check Basel')!;
    expect(steered).not.toBeNull();
    const current = chat.messages;
    chat.messages = [
      ...current.slice(0, -1),
      steered.userMessage,
      current[current.length - 1],
    ]; // F1: mid-stream setMessages

    // Backend acks the reroute to the sender only — swallowed, nothing rendered.
    wire.emit({ id: wire.sent[1].id, steering: true } as AgentResponseData);
    // The ORIGINAL stream continues after the mid-stream messages replacement:
    wire.emit(streamChunk(' it… and Basel.', 25));
    wire.emit(terminal());

    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    const finalMessages = chat.messages;
    expect(finalMessages.filter((m) => m.role === 'user').map(textOf)).toEqual([
      'start',
      'also check Basel',
    ]);
    expect(textOf(lastAssistant(chat))).toBe('Working on it… and Basel.');
    expect(finalMessages.filter((m) => m.role === 'assistant')).toHaveLength(1);
  });
});

describe('turn lifecycle extras', () => {
  it('backend error closes the turn with an error part and error status', async () => {
    const { wire, chat } = setup();
    void chat.sendMessage({ text: 'boom' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    // Errors arrive sid-only: no contextId, routed via the send id.
    wire.emit({ id: wire.sent[0].id, error: 'orchestrator exploded' });
    await vi.waitFor(() => expect(chat.status).toBe('error'));
    expect(chat.error?.message).toContain('orchestrator exploded');
  });

  it('stop() cancels the backend task and settles the stream', async () => {
    const { wire, chat } = setup();
    void chat.sendMessage({ text: 'long task' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    wire.emit(streamChunk('Working', 7));
    await chat.stop();
    await vi.waitFor(() => expect(wire.cancelled).toContain(CONV));
    await vi.waitFor(() => expect(chat.status).toBe('ready'));
  });

  it('fallback final answer supersedes a divergent streamed stub', async () => {
    const { wire, chat } = setup();
    void chat.sendMessage({ text: 'delegate' });
    await vi.waitFor(() => expect(wire.sent).toHaveLength(1));
    wire.emit(streamChunk('stub…', 5));
    wire.emit({
      contextId: CONV,
      kind: 'status-update',
      role: 'agent',
      metadata: { final_answer_source: 'fallback' },
      status: { state: 'completed', message: { parts: [{ kind: 'text', text: 'The real full answer.' }] } },
    } as AgentResponseData);
    await vi.waitFor(() => expect(chat.status).toBe('ready'));
    const msg = lastAssistant(chat)!;
    const textParts = msg.parts.filter((p) => p.type === 'text') as Array<{ text: string }>;
    // The stub was RETRACTED via reset-step; only the authoritative answer remains.
    expect(textParts.map((p) => p.text)).toEqual(['The real full answer.']);
  });
});
