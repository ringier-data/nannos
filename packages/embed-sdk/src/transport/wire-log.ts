/**
 * Wire log — the raw socket traffic (every received `agent_response` event and
 * every sent payload) behind the developer-mode inspector.
 *
 * Three sources, one merged timeline ordered by timestamp:
 *
 *   - LIVE: what this panel sees right now. A ring buffer of references to
 *     payloads that already exist, so the cost is one array slot each.
 *   - STORED: this browser's own record of earlier turns, kept in localStorage
 *     by `WireStore` (dev mode only — see wire-store.ts). Loaded per
 *     conversation via `hydrate()`, so a reload or a jump back to an older
 *     conversation still shows its traffic.
 *   - SERVER: the backend's persisted record, pulled on demand by
 *     `fetchWireHistory()` and handed to `replay()` — the only source for a
 *     conversation this browser never ran.
 *
 * The engine owns one instance per chat scope and exposes it as
 * `engine.wireLog`; the transport pushes into it. Nothing here renders — the
 * panel's dev inspector subscribes via `useSyncExternalStore`.
 */
import { generateUUID } from '../core/protocol';
import type { WireStore } from './wire-store';

export interface WireLogEntry {
  /** Stable across sessions: a stored entry keeps the id it was logged with,
   *  which is what de-duplicates a stored record against the live buffer. */
  id: string;
  seq: number;
  ts: number;
  dir: 'in' | 'out';
  conversationId?: string;
  /** Compact classification, e.g. 'status-update · working · activity-log'. */
  label: string;
  payload: unknown;
  /** Absent = seen live in this session. */
  source?: 'stored' | 'server';
}

/** An entry from a record rather than the live socket: the log stamps the
 *  ordering seq, and the id when the source has none of its own. */
export type ReplayEntry = Omit<WireLogEntry, 'seq' | 'id'> & { id?: string };

const CAPACITY = 300;
/** Per replayed record, and how many records stay in memory at once. */
const REPLAY_CAPACITY = 200;
const REPLAY_RECORDS = 4;

export class WireLog {
  private live: WireLogEntry[] = [];
  /** Loaded records, keyed `<source>:<conversationId>`, insertion-ordered. */
  private replayed = new Map<string, WireLogEntry[]>();
  /** Cached merge of the above — `useSyncExternalStore` needs a snapshot that
   *  only changes when the data does. */
  private merged: WireLogEntry[] = [];
  private seq = 0;
  /** Replayed entries sort BEFORE live ones at an equal timestamp. */
  private replaySeq = 0;
  private readonly nonce = generateUUID().slice(0, 8);
  private readonly listeners = new Set<() => void>();

  /** With a store, every live entry is mirrored to localStorage and
   *  `hydrate()` can read earlier sessions back. Dev mode only. */
  constructor(private readonly store?: WireStore) {}

  get persists(): boolean {
    return this.store !== undefined;
  }

  push(entry: Omit<WireLogEntry, 'seq' | 'ts' | 'id'>): void {
    this.seq += 1;
    const full: WireLogEntry = {
      ...entry,
      id: `${this.nonce}-${this.seq}`,
      seq: this.seq,
      ts: Date.now(),
    };
    const next = [...this.live, full];
    this.live = next.length > CAPACITY ? next.slice(next.length - CAPACITY) : next;
    this.store?.append(full);
    this.recompute();
  }

  /** Loads this browser's stored record of a conversation. Cheap and
   *  idempotent: a conversation already loaded is left alone. */
  hydrate(conversationId: string): void {
    if (!this.store || this.replayed.has(`stored:${conversationId}`)) return;
    this.load(`stored:${conversationId}`, this.store.read(conversationId));
  }

  /** Adds a fetched record (see `fetchWireHistory`) to the timeline. */
  replay(conversationId: string, entries: ReplayEntry[]): void {
    this.load(`server:${conversationId}`, entries);
  }

  /** True once the server record of a conversation has been pulled. */
  hasReplay(conversationId: string): boolean {
    return this.replayed.has(`server:${conversationId}`);
  }

  private load(key: string, entries: ReplayEntry[]): void {
    const capped =
      entries.length > REPLAY_CAPACITY ? entries.slice(entries.length - REPLAY_CAPACITY) : entries;
    this.replaySeq -= 1;
    const base = this.replaySeq;
    this.replayed.set(
      key,
      capped.map((e, i) => ({ ...e, id: e.id ?? `${key}-${i}`, seq: base })),
    );
    // Bounded memory: the least recently loaded record goes.
    while (this.replayed.size > REPLAY_RECORDS) {
      const oldest = this.replayed.keys().next().value;
      if (oldest === undefined) break;
      this.replayed.delete(oldest);
    }
    this.recompute();
  }

  clear(): void {
    this.live = [];
    this.replayed.clear();
    this.store?.clearAll();
    this.recompute();
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Stable reference between pushes (useSyncExternalStore contract). */
  getSnapshot = (): WireLogEntry[] => this.merged;

  /** Merge the sources into one timeline. An entry that is both live and in a
   *  record appears once, as the LIVE one — same id, and the live buffer holds
   *  the payload as it arrived rather than a shrunk-to-fit copy. Ordering is
   *  the timestamp sort below, so the iteration order here only decides which
   *  copy of a duplicate survives. */
  private recompute(): void {
    const seen = new Set<string>();
    const all: WireLogEntry[] = [];
    for (const list of [this.live, ...this.replayed.values()]) {
      for (const entry of list) {
        if (seen.has(entry.id)) continue;
        seen.add(entry.id);
        all.push(entry);
      }
    }
    all.sort((a, b) => a.ts - b.ts || a.seq - b.seq);
    this.merged = all;
    for (const fn of this.listeners) fn();
  }
}

/** 'urn:nannos:a2a:activity-log:1.0' → 'activity-log'. */
function shortExt(urn: string): string {
  const match = /^urn:nannos:a2a:([^:]+):/.exec(urn);
  return match ? match[1] : urn;
}

/** Compact one-line classification of a received agent_response event. */
export function labelAgentEvent(data: unknown): string {
  const d = data as {
    steering?: boolean;
    kind?: string;
    error?: string;
    role?: string;
    status?: { state?: unknown; message?: { extensions?: string[]; metadata?: { kind?: unknown } } };
    artifact?: unknown;
  };
  if (d.steering) return 'steering-ack';
  if (d.error) return 'error';
  const parts: string[] = [];
  if (d.kind) parts.push(d.kind);
  else if (d.status) parts.push('status');
  else if (d.artifact) parts.push('artifact');
  else if (d.role === 'agent') parts.push('agent-message');
  const state = d.status?.state;
  if (state != null) parts.push(String(state).replace(/^TASK_STATE_/i, '').toLowerCase());
  for (const ext of d.status?.message?.extensions ?? []) parts.push(shortExt(ext));
  // A mid-turn note shares the activity-log URN; the kind marker is what tells
  // the two apart on the wire, so the dev log says which one arrived.
  if (d.status?.message?.metadata?.kind === 'note') parts.push('note');
  return parts.join(' · ') || 'event';
}
