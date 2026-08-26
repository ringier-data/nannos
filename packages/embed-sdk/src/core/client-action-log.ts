/**
 * Client-action log — what the SDK actually DID with every directive the agent
 * sent: which delivery path carried it, whether the Zod guard accepted it, what
 * came back, and what the object registry held at that moment.
 *
 * Without this there is nothing to look at. A `navigate`/`highlight` directive
 * leaves no trace in the thread (it is not chat content), and an `apply` that
 * lands on a target the host never registered is refused SILENTLY as far as the
 * UI is concerned — the agent is told, the developer is not. Both paths funnel
 * through `NannosCore`, which is where the entries are made:
 *
 *   - `fire-and-forget` — navigate/highlight, executed by the core's own
 *     status-update listener. No result travels back to the agent.
 *   - `round-trip` — apply/read_current_page, where the agent's tool call is
 *     parked until `runClientAction` answers it.
 *
 * Always recorded (a few dozen small objects, no persistence), so a directive
 * that fired before anyone opened the dev inspector is still there to inspect.
 * Rendered by panel/components/dev-context-inspector.tsx; also reachable from
 * the browser console as `window.__nannos.clientActions` while dev mode is on.
 */
import type { ClientActionResult } from './client-action';

/** Which of the two extension shapes delivered the directive. */
export type ClientActionPath = 'fire-and-forget' | 'round-trip';

/** `refused` is an `{ok:false}` answer (invalid / unknown-target / unsupported)
 *  — the SDK worked as designed and said no; `threw` is a HOST handler that
 *  blew up, which is a bug in the integration rather than in the directive. */
export type ClientActionOutcome = 'pending' | 'ok' | 'refused' | 'threw';

export interface ClientActionLogEntry {
  id: string;
  seq: number;
  /** When the directive reached the executor. */
  ts: number;
  path: ClientActionPath;
  /** `directive.kind` when the raw payload carries a readable one, else null —
   *  an unparsable directive still gets a row, which is the whole point. */
  kind: string | null;
  /** `type:id` of the directive's target, for the kinds that have one. */
  target: string | null;
  directive: unknown;
  outcome: ClientActionOutcome;
  result?: ClientActionResult;
  /** Message of a host handler that threw. */
  error?: string;
  durationMs?: number;
  /** Registry keys at dispatch time — what an `unknown-target` refusal was
   *  matched against. The usual answer to "why didn't my apply land". */
  knownTargets: string[];
}

const CAPACITY = 50;

/** Read `kind`/`target` off a payload that has NOT been validated yet. */
function describe(directive: unknown): Pick<ClientActionLogEntry, 'kind' | 'target'> {
  const d = directive as { kind?: unknown; target?: { type?: unknown; id?: unknown } } | null;
  const kind = typeof d?.kind === 'string' ? d.kind : null;
  const t = d?.target;
  const target =
    t && typeof t.type === 'string' && typeof t.id === 'string' ? `${t.type}:${t.id}` : null;
  return { kind, target };
}

export class ClientActionLog {
  private entries: ClientActionLogEntry[] = [];
  private seq = 0;
  private readonly listeners = new Set<() => void>();

  /** Opens a `pending` row and returns its id — settle it with `settle`/`fail`.
   *  Logged BEFORE execution so a directive whose handler hangs (an async host
   *  `apply` that never resolves) is visible as pending rather than missing. */
  start(path: ClientActionPath, directive: unknown, knownTargets: string[]): string {
    this.seq += 1;
    const entry: ClientActionLogEntry = {
      id: `ca-${this.seq}`,
      seq: this.seq,
      ts: Date.now(),
      path,
      ...describe(directive),
      directive,
      outcome: 'pending',
      knownTargets,
    };
    const next = [...this.entries, entry];
    this.entries = next.length > CAPACITY ? next.slice(next.length - CAPACITY) : next;
    this.emit();
    return entry.id;
  }

  settle(id: string, result: ClientActionResult): void {
    this.update(id, (e) => ({
      ...e,
      outcome: result.ok ? 'ok' : 'refused',
      result,
      durationMs: Date.now() - e.ts,
    }));
  }

  fail(id: string, err: unknown): void {
    this.update(id, (e) => ({
      ...e,
      outcome: 'threw',
      error: err instanceof Error ? err.message : String(err),
      durationMs: Date.now() - e.ts,
    }));
  }

  clear(): void {
    this.entries = [];
    this.emit();
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Stable reference between changes (useSyncExternalStore contract). */
  getSnapshot = (): ClientActionLogEntry[] => this.entries;

  private update(id: string, fn: (e: ClientActionLogEntry) => ClientActionLogEntry): void {
    const i = this.entries.findIndex((e) => e.id === id);
    // Dropped by the capacity cap while its handler was in flight — nothing to
    // settle, and pushing it back would put a stale row at the head.
    if (i === -1) return;
    const next = [...this.entries];
    next[i] = fn(next[i]);
    this.entries = next;
    this.emit();
  }

  private emit(): void {
    for (const fn of this.listeners) fn();
  }
}
