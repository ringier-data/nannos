/**
 * Post-hoc wire traffic: the SERVER's record of a conversation, for the
 * dev inspector.
 *
 * console-backend keeps the original JSON of every event it persists —
 * `raw_payload` on each row of `/api/v1/messages/{conversationId}` (the send
 * payload on a user row, the agent_response on an agent row). So a
 * conversation this browser never saw — another machine, another day, before
 * dev mode was ever on — can still be inspected.
 *
 * It is a RECONSTRUCTION, not a recording: only events the backend persists
 * are there. Streamed text deltas are folded into one stored final, and
 * intermediate output is re-synthesized into a single artifact-update row
 * (app.py `_flush_intermediate_buffers`). For frame-by-frame truth, the local
 * record (wire-store.ts) of a conversation this browser actually ran is the
 * better source; the two merge by timestamp in the log.
 */
import { getTaskState } from '../core/protocol';
import type { RestMessageRow } from './history-mapper';
import { labelAgentEvent, serverWireId, type ReplayEntry } from './wire-log';

const DEFAULT_LIMIT = 100;

function tsOf(row: RestMessageRow): number {
  const raw = row.created_at ?? row.timestamp ?? row.sort_key;
  const t = raw ? new Date(raw as string).getTime() : NaN;
  return Number.isNaN(t) ? 0 : t;
}

/** The stored wire payload, or `undefined` when the row has none. */
function storedPayload(row: RestMessageRow): unknown | undefined {
  if (typeof row.raw_payload !== 'string' || !row.raw_payload) return undefined;
  try {
    return JSON.parse(row.raw_payload);
  } catch {
    return row.raw_payload;
  }
}

/**
 * Label built from the ROW, for a row the backend stored without a payload.
 *
 * `labelAgentEvent` cannot name one: it would read the placeholder below and
 * answer its catch-all 'event'. That is how a duplicated answer hid in this log
 * — the assembled-artifact half of the pair read as a nameless "event" while
 * only its echo said `input_required`, so the log looked like it held one answer
 * when it held two. The row still knows its own kind and state, so use those.
 */
function labelRow(row: RestMessageRow): string {
  const state = getTaskState(row.state);
  return [row.kind || 'event', state].filter(Boolean).join(' · ');
}

function rowToEntry(row: RestMessageRow, conversationId: string): ReplayEntry {
  const stored = storedPayload(row);
  const out = row.role === 'user';
  return {
    // The row's own id, so a part the history mapper stamped with the same
    // key resolves this entry exactly (see `serverWireId`).
    id: serverWireId(row),
    ts: tsOf(row),
    dir: out ? 'out' : 'in',
    conversationId,
    label: out ? 'send-message' : stored ? labelAgentEvent(stored) : labelRow(row),
    // No stored payload: the row itself is what the server has.
    payload: stored ?? { '[no raw_payload]': true, row },
    source: 'server',
  };
}

/**
 * The stored traffic of one conversation, oldest first. `null` means the
 * lookup failed (the inspector says so rather than showing an empty log).
 */
export async function fetchWireHistory(
  fetcher: (path: string, init?: RequestInit) => Promise<Response>,
  conversationId: string,
  limit: number = DEFAULT_LIMIT,
): Promise<ReplayEntry[] | null> {
  try {
    const resp = await fetcher(
      `/api/v1/messages/${encodeURIComponent(conversationId)}?limit=${limit}`,
    );
    // 404 = a conversation the server never stored — an empty record, not a failure.
    if (resp.status === 404) return [];
    if (!resp.ok) return null;
    const data = (await resp.json()) as Record<string, unknown>;
    const rows = Array.isArray(data.items)
      ? (data.items as RestMessageRow[])
      : Array.isArray(data.messages)
        ? (data.messages as RestMessageRow[])
        : [];
    return rows.map((row) => rowToEntry(row, conversationId)).sort((a, b) => a.ts - b.ts);
  } catch {
    return null;
  }
}
