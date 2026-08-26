/**
 * Wire log — a small ring buffer of the raw socket traffic (every received
 * `agent_response` event and every sent payload), for the developer-mode
 * inspector. Always on: entries hold REFERENCES to payloads that already
 * exist, so the cost is one array slot each; the buffer caps total retention.
 *
 * The engine owns one instance per chat scope and exposes it as
 * `engine.wireLog`; the transport pushes into it. Nothing here renders — the
 * panel's dev inspector subscribes via `useSyncExternalStore`.
 */

export interface WireLogEntry {
  seq: number;
  ts: number;
  dir: 'in' | 'out';
  conversationId?: string;
  /** Compact classification, e.g. 'status-update · working · activity-log'. */
  label: string;
  payload: unknown;
}

const CAPACITY = 300;

export class WireLog {
  private entries: WireLogEntry[] = [];
  private seq = 0;
  private readonly listeners = new Set<() => void>();

  push(entry: Omit<WireLogEntry, 'seq' | 'ts'>): void {
    this.seq += 1;
    const next = [...this.entries, { ...entry, seq: this.seq, ts: Date.now() }];
    this.entries = next.length > CAPACITY ? next.slice(next.length - CAPACITY) : next;
    for (const fn of this.listeners) fn();
  }

  clear(): void {
    this.entries = [];
    for (const fn of this.listeners) fn();
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Stable reference between pushes (useSyncExternalStore contract). */
  getSnapshot = (): WireLogEntry[] => this.entries;
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
    status?: { state?: unknown; message?: { extensions?: string[] } };
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
  return parts.join(' · ') || 'event';
}
