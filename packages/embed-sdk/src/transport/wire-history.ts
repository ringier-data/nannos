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
import type { RestMessageRow } from './history-mapper';
import { labelAgentEvent, type ReplayEntry } from './wire-log';

const DEFAULT_LIMIT = 100;

function tsOf(row: RestMessageRow): number {
  const raw = row.created_at ?? row.timestamp ?? row.sort_key;
  const t = raw ? new Date(raw as string).getTime() : NaN;
  return Number.isNaN(t) ? 0 : t;
}

function payloadOf(row: RestMessageRow): unknown {
  if (typeof row.raw_payload === 'string' && row.raw_payload) {
    try {
      return JSON.parse(row.raw_payload);
    } catch {
      return row.raw_payload;
    }
  }
  // No stored payload (older rows): the row itself is what the server has.
  return { '[no raw_payload]': true, row };
}

function rowToEntry(row: RestMessageRow, conversationId: string): ReplayEntry {
  const payload = payloadOf(row);
  const out = row.role === 'user';
  return {
    ts: tsOf(row),
    dir: out ? 'out' : 'in',
    conversationId,
    label: out ? 'send-message' : labelAgentEvent(payload) || row.kind || 'event',
    payload,
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
