/**
 * Dev-mode persistence for the wire log: the raw traffic of a conversation
 * survives a reload, a conversation switch, and a panel remount, so a
 * developer can open a conversation from the history list and still inspect
 * what actually crossed the wire while it ran.
 *
 * localStorage, one key per conversation (`nannos:wire:<conversationId>`),
 * written from a debounced buffer — a streaming turn pushes many frames a
 * second and each write re-serializes the whole conversation.
 *
 * ONLY constructed when dev mode is available (see panel/dev-mode.tsx): wire
 * payloads are the full request/response bodies, which have no business
 * sitting in an end user's browser storage.
 *
 * Everything here is best-effort. A browser with storage disabled, a full
 * quota, or a payload too large to keep costs the developer some history —
 * never a thrown error on the live path.
 */
import type { WireLogEntry } from './wire-log';

const PREFIX = 'nannos:wire:';
const VERSION = 1;

/** Per conversation: newest N entries. */
const MAX_ENTRIES = 150;
/** How many conversations keep a record; least recently written evicted. */
const MAX_CONVERSATIONS = 12;
/** A single payload above this is stored as a marker instead — one huge
 *  artifact must not push a whole conversation out of storage. */
const MAX_ENTRY_BYTES = 48_000;
/** Frames are batched this long before the record is rewritten. */
const FLUSH_MS = 400;

interface StoredRecord {
  v: number;
  updatedAt: number;
  entries: WireLogEntry[];
}

function keyOf(conversationId: string): string {
  return `${PREFIX}${conversationId}`;
}

function readRaw(key: string): StoredRecord | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredRecord;
    if (parsed?.v !== VERSION || !Array.isArray(parsed.entries)) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** Serializes one entry, replacing a payload too big (or cyclic) to keep. */
function shrink(entry: WireLogEntry): WireLogEntry {
  let size = 0;
  try {
    size = JSON.stringify(entry.payload)?.length ?? 0;
  } catch {
    return { ...entry, payload: { '[not stored]': 'payload is not serializable' } };
  }
  if (size <= MAX_ENTRY_BYTES) return entry;
  return { ...entry, payload: { '[not stored]': `payload too large (${size} bytes)` } };
}

export class WireStore {
  /** Not yet written frames, per conversation. */
  private pending = new Map<string, WireLogEntry[]>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  /** Set after a write fails for good — stop burning time on every frame. */
  private off = false;

  constructor() {
    // A reload during a streaming turn must not drop the last frames.
    if (typeof window !== 'undefined') {
      window.addEventListener('pagehide', () => this.flush());
    }
  }

  /** Queues one entry. Entries the transport could not attribute to a
   *  conversation are dropped — there is no record to file them under. */
  append(entry: WireLogEntry): void {
    if (this.off || !entry.conversationId) return;
    const list = this.pending.get(entry.conversationId);
    if (list) list.push(entry);
    else this.pending.set(entry.conversationId, [entry]);
    if (this.timer === null) {
      this.timer = setTimeout(() => {
        this.timer = null;
        this.flush();
      }, FLUSH_MS);
    }
  }

  /** Everything stored for one conversation, oldest first. Entries are marked
   *  `source: 'stored'` — they are this browser's own record, not live. */
  read(conversationId: string): WireLogEntry[] {
    if (typeof localStorage === 'undefined') return [];
    const record = readRaw(keyOf(conversationId));
    if (!record) return [];
    return record.entries.map((e) => ({ ...e, source: 'stored' as const }));
  }

  /** Drops every stored conversation (the inspector's clear button). */
  clearAll(): void {
    this.pending.clear();
    for (const key of this.storedKeys()) {
      try {
        localStorage.removeItem(key);
      } catch {
        /* ignore */
      }
    }
  }

  private storedKeys(): string[] {
    const keys: string[] = [];
    try {
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i);
        if (key?.startsWith(PREFIX)) keys.push(key);
      }
    } catch {
      /* ignore */
    }
    return keys;
  }

  /** Writes every buffered conversation; prunes on quota errors. */
  private flush(): void {
    if (this.off || this.pending.size === 0) return;
    const batches = [...this.pending];
    this.pending.clear();
    for (const [conversationId, entries] of batches) {
      this.write(conversationId, entries);
    }
    this.evictConversations();
  }

  private write(conversationId: string, incoming: WireLogEntry[]): void {
    const key = keyOf(conversationId);
    const existing = readRaw(key)?.entries ?? [];
    let entries = [...existing, ...incoming.map(shrink)];
    if (entries.length > MAX_ENTRIES) entries = entries.slice(entries.length - MAX_ENTRIES);

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const record: StoredRecord = { v: VERSION, updatedAt: Date.now(), entries };
      try {
        localStorage.setItem(key, JSON.stringify(record));
        return;
      } catch {
        // Quota (or a payload that would not serialize): make room and retry —
        // first by dropping OTHER conversations, then this record's own tail.
        if (attempt === 0 && this.evictOldest(key)) continue;
        if (entries.length > 8) {
          entries = entries.slice(Math.ceil(entries.length / 2));
          continue;
        }
        this.off = true;
        return;
      }
    }
  }

  /** Removes the least recently written record other than `keep`. */
  private evictOldest(keep: string): boolean {
    let oldest: string | null = null;
    let oldestAt = Number.POSITIVE_INFINITY;
    for (const key of this.storedKeys()) {
      if (key === keep) continue;
      const at = readRaw(key)?.updatedAt ?? 0;
      if (at < oldestAt) {
        oldestAt = at;
        oldest = key;
      }
    }
    if (!oldest) return false;
    try {
      localStorage.removeItem(oldest);
      return true;
    } catch {
      return false;
    }
  }

  /** Keeps the record count bounded between quota errors. */
  private evictConversations(): void {
    const keys = this.storedKeys();
    if (keys.length <= MAX_CONVERSATIONS) return;
    const byAge = keys
      .map((key) => ({ key, at: readRaw(key)?.updatedAt ?? 0 }))
      .sort((a, b) => a.at - b.at);
    for (const { key } of byAge.slice(0, keys.length - MAX_CONVERSATIONS)) {
      try {
        localStorage.removeItem(key);
      } catch {
        /* ignore */
      }
    }
  }
}
