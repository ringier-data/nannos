// @vitest-environment happy-dom
/**
 * The wire log's three sources and how they merge: live traffic, this
 * browser's stored record (dev mode only), and the backend's persisted one.
 * The regression that matters: a stored record read back into the SAME log
 * must not duplicate the live entries it was written from.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WireLog } from './wire-log';
import { WireStore } from './wire-store';
import { fetchWireHistory } from './wire-history';

const FLUSH_MS = 400;

function flushStore() {
  vi.advanceTimersByTime(FLUSH_MS + 1);
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('WireLog without a store (end-user default)', () => {
  it('logs live traffic and writes nothing to localStorage', () => {
    const log = new WireLog();
    log.push({ dir: 'out', conversationId: 'c1', label: 'send-message', payload: { a: 1 } });
    flushStore();

    expect(log.persists).toBe(false);
    expect(log.getSnapshot()).toHaveLength(1);
    expect(localStorage.length).toBe(0);
  });
});

describe('WireLog with a store (dev mode)', () => {
  it('reads an earlier session back, marked as stored', () => {
    const first = new WireLog(new WireStore());
    first.push({ dir: 'out', conversationId: 'c1', label: 'send-message', payload: { text: 'hi' } });
    first.push({ dir: 'in', conversationId: 'c1', label: 'status · working', payload: { ok: true } });
    flushStore();

    // A reload: a brand-new log, nothing live in it.
    const next = new WireLog(new WireStore());
    expect(next.getSnapshot()).toHaveLength(0);
    next.hydrate('c1');

    const entries = next.getSnapshot();
    expect(entries.map((e) => e.label)).toEqual(['send-message', 'status · working']);
    expect(entries.every((e) => e.source === 'stored')).toBe(true);
  });

  it('keeps records apart per conversation', () => {
    const log = new WireLog(new WireStore());
    log.push({ dir: 'out', conversationId: 'c1', label: 'one', payload: {} });
    log.push({ dir: 'out', conversationId: 'c2', label: 'two', payload: {} });
    flushStore();

    const next = new WireLog(new WireStore());
    next.hydrate('c2');
    expect(next.getSnapshot().map((e) => e.label)).toEqual(['two']);
  });

  it('does not duplicate live entries when its own record is read back', () => {
    const log = new WireLog(new WireStore());
    log.push({ dir: 'out', conversationId: 'c1', label: 'send-message', payload: {} });
    flushStore();

    log.hydrate('c1');
    expect(log.getSnapshot()).toHaveLength(1);
    // The live one wins: it is the same entry, not a replayed copy.
    expect(log.getSnapshot()[0].source).toBeUndefined();
  });

  it('stores a marker instead of an oversized payload', () => {
    const log = new WireLog(new WireStore());
    log.push({ dir: 'in', conversationId: 'c1', label: 'artifact', payload: { big: 'x'.repeat(60_000) } });
    flushStore();

    const next = new WireLog(new WireStore());
    next.hydrate('c1');
    expect(JSON.stringify(next.getSnapshot()[0].payload)).toContain('too large');
  });

  it('clear drops the live log and every stored conversation', () => {
    const log = new WireLog(new WireStore());
    log.push({ dir: 'out', conversationId: 'c1', label: 'one', payload: {} });
    flushStore();

    log.clear();
    expect(log.getSnapshot()).toHaveLength(0);
    expect(localStorage.length).toBe(0);
  });
});

describe('fetchWireHistory (the backend record)', () => {
  const rows = [
    {
      role: 'assistant',
      created_at: '2026-01-01T10:00:01.000Z',
      raw_payload: JSON.stringify({ kind: 'status-update', status: { state: 'TASK_STATE_WORKING' } }),
    },
    {
      role: 'user',
      created_at: '2026-01-01T10:00:00.000Z',
      raw_payload: JSON.stringify({ message: { messageId: 'm1' } }),
    },
  ];

  const fetcher = (body: unknown, status = 200) =>
    vi.fn(async () => new Response(JSON.stringify(body), { status })) as unknown as (
      path: string,
    ) => Promise<Response>;

  it('maps rows to wire entries, oldest first, with direction from the role', async () => {
    const entries = await fetchWireHistory(fetcher({ messages: rows }), 'c1');
    expect(entries?.map((e) => [e.dir, e.label])).toEqual([
      ['out', 'send-message'],
      ['in', 'status-update · working'],
    ]);
    expect(entries?.every((e) => e.source === 'server')).toBe(true);
  });

  it('merges into the live timeline by timestamp', async () => {
    const log = new WireLog();
    const entries = await fetchWireHistory(fetcher({ messages: rows }), 'c1');
    log.replay('c1', entries!);
    log.push({ dir: 'out', conversationId: 'c1', label: 'live-send', payload: {} });

    expect(log.getSnapshot().map((e) => e.label)).toEqual([
      'send-message',
      'status-update · working',
      'live-send',
    ]);
    expect(log.hasReplay('c1')).toBe(true);
  });

  it('treats a 404 as an empty record and any other failure as null', async () => {
    expect(await fetchWireHistory(fetcher({}, 404), 'c1')).toEqual([]);
    expect(await fetchWireHistory(fetcher({}, 500), 'c1')).toBeNull();
  });
});
