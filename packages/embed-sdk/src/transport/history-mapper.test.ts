import { describe, expect, it } from 'vitest';
import { ACTIVITY_LOG_EXT, HITL_EXT, INTERMEDIATE_OUTPUT_EXT } from '../core/extensions';
import { textArrivalTs } from './ai-types';
import {
  appendRestoredInterrupt,
  findPendingInterrupt,
  rowsToUIMessages,
  type RestMessageRow,
} from './history-mapper';

const t = (i: number) => new Date(1700000000000 + i * 1000).toISOString();

const userRow = (i: number, text: string, metadata?: Record<string, unknown>): RestMessageRow => ({
  id: `u${i}`,
  role: 'user',
  content: text,
  created_at: t(i),
  ...(metadata && { metadata }),
});

const finalRow = (i: number, text: string): RestMessageRow => ({
  id: `a${i}`,
  role: 'agent',
  kind: 'status-update',
  state: 'completed',
  content: text,
  parts: [{ kind: 'text', text }],
  created_at: t(i),
});

const activityRow = (i: number, text: string, source?: string): RestMessageRow => ({
  id: `act${i}`,
  role: 'agent',
  kind: 'status-update',
  state: 'working',
  parts: [{ kind: 'text', text }],
  created_at: t(i),
  raw_payload: JSON.stringify({
    status: {
      message: {
        extensions: [ACTIVITY_LOG_EXT],
        parts: [{ kind: 'text', text }],
        ...(source && { metadata: { source } }),
      },
    },
  }),
});

/** A mid-turn note is an activity row whose message metadata carries kind='note'. */
const noteRow = (i: number, text: string): RestMessageRow => ({
  id: `note${i}`,
  role: 'agent',
  kind: 'status-update',
  state: 'working',
  parts: [{ kind: 'text', text }],
  created_at: t(i),
  raw_payload: JSON.stringify({
    status: {
      message: {
        extensions: [ACTIVITY_LOG_EXT],
        parts: [{ kind: 'text', text }],
        metadata: { kind: 'note' },
      },
    },
  }),
});

const thoughtRow = (i: number, agent: string, text: string): RestMessageRow => ({
  id: `th${i}`,
  role: 'agent',
  kind: 'artifact-update',
  parts: [{ kind: 'text', text }],
  created_at: t(i),
  raw_payload: JSON.stringify({
    artifact: {
      extensions: [INTERMEDIATE_OUTPUT_EXT],
      metadata: { agent_name: agent },
      parts: [{ kind: 'text', text }],
    },
  }),
});

const hitlRow = (i: number, callId: string): RestMessageRow => ({
  id: `h${i}`,
  role: 'agent',
  kind: 'status-update',
  state: 'input-required',
  created_at: t(i),
  raw_payload: JSON.stringify({
    status: {
      message: {
        extensions: [HITL_EXT],
        parts: [
          { kind: 'text', text: 'Please review' },
          {
            kind: 'data',
            data: {
              action_requests: [{ name: 'book_flight', args: { _call_id: callId, city: 'Zürich' } }],
              review_configs: [{ action_name: 'book_flight', allowed_decisions: ['approve', 'reject'] }],
            },
          },
        ],
      },
    },
  }),
});

describe('rowsToUIMessages', () => {
  it('folds agent rows between user rows into ONE assistant message with ordered parts', () => {
    const rows = [
      userRow(0, 'hello'),
      activityRow(1, 'Calling tool…', 'billing-agent'),
      thoughtRow(2, 'researcher', 'thinking hard'),
      finalRow(3, 'The answer.'),
      userRow(4, 'thanks'),
      finalRow(5, 'Anytime.'),
    ];
    const messages = rowsToUIMessages(rows);
    expect(messages.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant']);

    const first = messages[1];
    expect(first.parts.map((p) => p.type)).toEqual(['data-activity', 'data-agent-thought', 'text']);
    expect(first.id).toBe('a3'); // the persisted row id becomes the message id
    expect(first.metadata?.persistedMessageId).toBe('a3');
    const activity = first.parts[0] as { data: { text: string; source?: string } };
    expect(activity.data).toMatchObject({ text: 'Calling tool…', source: 'billing-agent' });
    const thought = first.parts[1] as { data: { agent: string; complete: boolean } };
    expect(thought.data).toMatchObject({ agent: 'researcher', complete: true });
    // The answer keeps the row's time, so a reloaded turn reads as the same
    // dev-mode timeline as the live one.
    expect(textArrivalTs(first.parts[2])).toBe(new Date(t(3)).getTime());
  });

  it('keeps a mid-turn note apart from a tool line across a reload', () => {
    // The kind marker is the only thing separating the two on the wire; lose it
    // here and a reloaded turn shows the agent's own words as machine chatter.
    const messages = rowsToUIMessages([
      userRow(0, 'check campaign 456'),
      noteRow(1, 'Understood — running a health check on campaign 456.'),
      activityRow(2, 'Running search…'),
      finalRow(3, 'All good.'),
    ]);
    const parts = messages[1].parts as Array<{ type: string; data?: { text: string; kind?: string } }>;
    expect(parts.map((p) => p.type)).toEqual(['data-activity', 'data-activity', 'text']);
    expect(parts[0].data).toMatchObject({
      text: 'Understood — running a health check on campaign 456.',
      kind: 'note',
    });
    expect(parts[1].data?.kind).toBeUndefined();
  });

  it('restores a persisted context chip (injectedDisplayText) as display metadata', () => {
    const messages = rowsToUIMessages([
      userRow(0, 'the full instrumentation prompt', { injectedDisplayText: 'Suggest actions' }),
    ]);
    expect(messages[0].metadata?.display).toEqual({ kind: 'context', label: 'Suggest actions' });
    expect(messages[0].parts[0]).toMatchObject({ type: 'text', text: 'the full instrumentation prompt' });
  });

  it('drops task rows and rows with nothing displayable; sorts by time', () => {
    const rows: RestMessageRow[] = [
      finalRow(2, 'answer'),
      { id: 'task1', role: 'agent', kind: 'task', created_at: t(1) },
      userRow(0, 'q'),
    ];
    const messages = rowsToUIMessages(rows);
    expect(messages.map((m) => m.role)).toEqual(['user', 'assistant']);
  });

  it('a HITL-resume row (empty user message) renders nothing and does not split the turn', () => {
    const resumeRow: RestMessageRow = {
      id: 'u-resume',
      role: 'user',
      content: '',
      created_at: t(3),
      raw_payload: JSON.stringify({ message: '', dataParts: [{ decisions: [{ id: 'call-1', approved: true }] }] }),
    };
    const rows = [
      userRow(0, 'read my memory'),
      activityRow(1, 'Running ls…'),
      resumeRow,
      activityRow(4, 'Running read_file…'),
      finalRow(5, 'Here is what I found.'),
    ];
    const messages = rowsToUIMessages(rows);
    expect(messages.map((m) => m.role)).toEqual(['user', 'assistant']);
    expect(messages[1].parts.map((p) => p.type)).toEqual(['data-activity', 'data-activity', 'text']);
  });

  it('working-state progress rows become activity parts', () => {
    const rows = [
      userRow(0, 'call them'),
      {
        id: 'w1',
        role: 'agent',
        kind: 'status-update',
        state: 'working',
        parts: [{ kind: 'text', text: 'Call ringing…' }],
        created_at: t(1),
      } as RestMessageRow,
      finalRow(2, 'Done.'),
    ];
    const [, assistant] = rowsToUIMessages(rows);
    expect(assistant.parts[0]).toMatchObject({ type: 'data-activity', data: { text: 'Call ringing…' } });
  });
});

describe('findPendingInterrupt + appendRestoredInterrupt', () => {
  it('detects an unresolved trailing interrupt and appends live-identical approval parts', () => {
    const rows = [userRow(0, 'book it'), finalRow(1, 'Let me confirm.'), hitlRow(2, 'call-9')];
    const interrupt = findPendingInterrupt(rows)!;
    expect(interrupt.actionRequests).toHaveLength(1);

    const messages = appendRestoredInterrupt(rowsToUIMessages(rows), interrupt);
    const last = messages[messages.length - 1];
    expect(last.role).toBe('assistant');
    const tool = last.parts.find((p) => p.type === 'dynamic-tool') as {
      state: string;
      toolCallId: string;
      approval: { id: string };
    };
    expect(tool).toMatchObject({ state: 'approval-requested', toolCallId: 'call-9' });
    expect(tool.approval.id).toBe('call-9');
    expect(last.metadata?.hitl?.reviewConfigs).toHaveLength(1);
  });

  it('the risk-gate status text never renders — the card speaks for it', () => {
    const RISK = "Tool 'client_action' has risk score 0.90 (threshold: 0.80)";
    // What the REST endpoint really returns: the gate's text is also mirrored
    // into the row's own `content`/`parts` columns.
    const riskRow = { ...hitlRow(1, 'call-9'), content: RISK, parts: [{ kind: 'text', text: RISK }] };

    // Still open → the approval card, and no text part.
    const open = [userRow(0, 'create the campaign'), riskRow];
    const pending = rowsToUIMessages(open);
    expect(pending.flatMap((m) => m.parts).some((p) => p.type === 'text' && p.text.includes('risk score'))).toBe(false);

    // Answered → the row leaves no trace at all.
    const answered = rowsToUIMessages([...open, finalRow(2, 'Campaign created.')]);
    const texts = answered.flatMap((m) => m.parts).filter((p) => p.type === 'text') as Array<{ text: string }>;
    expect(texts.map((p) => p.text)).toEqual(['create the campaign', 'Campaign created.']);
  });

  it('an interrupt resolved by a LATER status is not restored', () => {
    const rows = [userRow(0, 'book it'), hitlRow(1, 'call-9'), finalRow(2, 'Booked!')];
    expect(findPendingInterrupt(rows)).toBeNull();
  });

  it('no interrupt in plain history', () => {
    expect(findPendingInterrupt([userRow(0, 'q'), finalRow(1, 'a')])).toBeNull();
  });
});

describe('duplicated answer rows', () => {
  it('folds the same answer persisted under several rows into ONE text part', () => {
    const text = 'Hello! How can I help you today?';
    const sameAnswerRow = (i: number, id: string, kind: string): RestMessageRow => ({
      id,
      role: 'agent',
      kind,
      content: text,
      parts: [{ kind: 'text', text }],
      created_at: t(i),
    });
    const messages = rowsToUIMessages([
      userRow(0, 'hi'),
      // Streamed final (artifact), full agent message, terminal status: one
      // answer, three rows, three ids — the thread must show ONE bubble.
      sameAnswerRow(1, 'art1', 'artifact-update'),
      sameAnswerRow(2, 'msg1', 'message'),
      finalRow(3, text),
    ]);
    const assistant = messages[messages.length - 1];
    expect(assistant.role).toBe('assistant');
    const textParts = assistant.parts.filter((p) => p.type === 'text');
    expect(textParts).toHaveLength(1);
    expect((textParts[0] as { text: string }).text).toBe(text);
    // The LAST persisted row names the turn, matching the live finalize.
    expect(assistant.id).toBe('a3');
  });

  it('lets an extending final supersede the shorter streamed text in place', () => {
    const messages = rowsToUIMessages([
      userRow(0, 'hi'),
      finalRow(1, 'The answer'),
      finalRow(2, 'The answer, with the rest of it.'),
    ]);
    const assistant = messages[messages.length - 1];
    const textParts = assistant.parts.filter((p) => p.type === 'text');
    expect(textParts).toHaveLength(1);
    expect((textParts[0] as { text: string }).text).toBe('The answer, with the rest of it.');
  });
});

describe('auth-required rows', () => {
  const AUTH_URL = 'https://gatana.nannos.ringier.ch/api/v1/mcp-servers/oauth/gt_7MOP0ckoiO/begin';
  const GATEWAY_TEXT =
    'This tool requires secondary authorization. You must tell the end-user to please go to ' +
    `the authorizeUrl.\n\nPlease visit the following URL to complete authentication:\n${AUTH_URL}`;

  const authRow = (i: number): RestMessageRow => ({
    id: `auth${i}`,
    role: 'agent',
    kind: 'status-update',
    // What the REST endpoint actually sends: the protobuf TaskState INT.
    state: 8,
    parts: [{ kind: 'text', text: GATEWAY_TEXT }],
    created_at: t(i),
    raw_payload: JSON.stringify({
      status: { message: { parts: [{ kind: 'text', text: GATEWAY_TEXT }] } },
      metadata: { auth_url: AUTH_URL, tool: 'eval', requires_auth: true },
    }),
  });

  it('restore as the structured part, not as the gateway text (int state)', () => {
    const messages = rowsToUIMessages([userRow(0, 'any notifications?'), authRow(1)]);
    const assistant = messages[messages.length - 1];
    expect(assistant.role).toBe('assistant');
    expect(assistant.parts).toEqual([
      {
        type: 'data-auth-required',
        id: 'hist-auth-1',
        data: {
          authUrl: AUTH_URL,
          tool: 'eval',
          message: GATEWAY_TEXT,
          // Dev-mode provenance: the stored event's wire label, and the row id
          // the wire replay shares (`serverWireId`).
          wire: 'status',
          wireId: 'srv:auth1',
        },
      },
    ]);
    // The agent-directed sentence must never come back as a text bubble.
    expect(assistant.parts.some((p) => p.type === 'text')).toBe(false);
  });
});
