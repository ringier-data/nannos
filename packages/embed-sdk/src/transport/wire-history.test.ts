/**
 * The dev inspector's "load from server" path: the backend's stored record of a
 * conversation, turned into wire-log entries.
 *
 * It must be a FAITHFUL view — one entry per stored row, nothing folded,
 * nothing dropped. The inspector is where a duplicated or missing event is
 * diagnosed, so any tidying here would hide the very bug it is used to find.
 */
import { describe, expect, it } from 'vitest';
import { fetchWireHistory } from './wire-history';
import type { RestMessageRow } from './history-mapper';

function serving(rows: RestMessageRow[], status = 200) {
  const body = JSON.stringify({ messages: rows });
  return async () => new Response(body, { status });
}

const ANSWER = 'Would you like to set up this campaign, or something else?';

/** The shape a turn leaves behind when the answer streamed and then paused for
 *  input: the assembled artifact, then the terminal status. */
const TURN: RestMessageRow[] = [
  {
    message_id: 'u1',
    role: 'user',
    kind: '',
    parts: [{ kind: 'text', text: 'hello' }],
    created_at: '2026-08-27T13:40:41.929Z',
    raw_payload: JSON.stringify({ id: 'u1', message: 'hello' }),
  },
  {
    message_id: 'a-artifact',
    role: 'assistant',
    kind: 'artifact-update',
    state: 3,
    parts: [{ kind: 'text', text: ANSWER }],
    created_at: '2026-08-27T13:40:51.998Z',
    // The backend assembled this from stream chunks; older rows kept no payload.
    raw_payload: null,
  },
  {
    message_id: 'a-status',
    role: 'assistant',
    kind: 'status-update',
    state: 6,
    parts: [{ kind: 'text', text: ANSWER }],
    created_at: '2026-08-27T13:40:52.021Z',
    raw_payload: JSON.stringify({
      kind: 'status-update',
      status: { state: 'TASK_STATE_INPUT_REQUIRED', message: { parts: [{ text: ANSWER }] } },
    }),
  },
];

describe('fetchWireHistory', () => {
  it('emits one entry per stored row, in order, folding nothing', async () => {
    const entries = (await fetchWireHistory(serving(TURN), 'conv-1'))!;
    expect(entries).toHaveLength(TURN.length);
    expect(entries.map((e) => e.dir)).toEqual(['out', 'in', 'in']);
    expect(entries.map((e) => e.id)).toEqual(['srv:u1', 'srv:a-artifact', 'srv:a-status']);
  });

  it('names a payload-less row from the row itself, never "event"', async () => {
    // Regression: `labelAgentEvent` answers its catch-all 'event' for the
    // placeholder payload, so the assembled half of a duplicated answer read as
    // a nameless "event" while only its echo said input_required — the log
    // looked like it held ONE answer when it held two.
    const entries = (await fetchWireHistory(serving(TURN), 'conv-1'))!;
    const assembled = entries.find((e) => e.id === 'srv:a-artifact')!;
    expect(assembled.label).toBe('artifact-update · completed');
    expect(entries.map((e) => e.label)).toEqual([
      'send-message',
      'artifact-update · completed',
      'status-update · input_required',
    ]);
  });

  it('prefers the stored payload when the backend kept one', async () => {
    const rows = structuredClone(TURN);
    rows[1].raw_payload = JSON.stringify({
      kind: 'artifact-update',
      artifact: { parts: [{ kind: 'text', text: ANSWER }] },
      assembledByConsole: true,
    });
    const entries = (await fetchWireHistory(serving(rows), 'conv-1'))!;
    const assembled = entries.find((e) => e.id === 'srv:a-artifact')!;
    expect(assembled.label).toBe('artifact-update');
    expect((assembled.payload as { assembledByConsole?: boolean }).assembledByConsole).toBe(true);
  });

  it('reads a conversation the server never stored as empty, not failed', async () => {
    await expect(fetchWireHistory(serving([], 404), 'conv-1')).resolves.toEqual([]);
  });

  it('reports a failed lookup as null, so the inspector can say so', async () => {
    await expect(fetchWireHistory(serving([], 500), 'conv-1')).resolves.toBeNull();
  });
});
