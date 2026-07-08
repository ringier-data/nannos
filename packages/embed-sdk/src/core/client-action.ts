import type { ObjectRegistry } from './registry';
import type { ApplyResult } from './types';
import { clientActionDirective } from './schemas';
import { CLIENT_ACTION_EXT } from './extensions';

export interface ClientActionDeps {
  registry: ObjectRegistry;
  /** Host-provided navigation (e.g. react-router). */
  navigate?: (to: string) => void;
  /** Host-provided highlight hook (scroll-into-view / outline a field). */
  highlight?: (target: { type: string; id: string }, field?: string) => void;
  /** Notified after an `apply` with which fields landed vs. were rejected, so the
   *  host can surface "couldn't apply X". Absent → rejections are console.warn'd
   *  (never silent). */
  onApplyResult?: (target: { type: string; id: string }, result: ApplyResult) => void;
}

export type ClientActionResult =
  | { ok: true; applied?: string[]; rejected?: ApplyResult['rejected'] }
  | { ok: false; reason: 'invalid' | 'unknown-target' };

/**
 * Sandboxed executor of `urn:nannos:a2a:client-action` directives. It runs ONLY
 * against handles the host registered; an unknown target is refused, not guessed.
 *
 * There is NO confirm layer here: approval for consequential actions (an `apply`)
 * happens ONCE, upstream, at the agent's tool-call HITL gate (the `client_action`
 * tool is risk-scored by kind — see tool_risk_scorer). A directive that reaches
 * the SDK has already been approved, so we apply it directly. The `confirm` field
 * on the directive is therefore ignored.
 */
export async function executeClientAction(
  raw: unknown,
  deps: ClientActionDeps,
): Promise<ClientActionResult> {
  const parsed = clientActionDirective.safeParse(raw);
  if (!parsed.success) return { ok: false, reason: 'invalid' };
  const directive = parsed.data;

  switch (directive.kind) {
    case 'apply': {
      const handle = deps.registry.get(directive.target.type, directive.target.id);
      if (!handle) return { ok: false, reason: 'unknown-target' };
      // Await: custom (plain-JS) handles may be async — a returned Promise must not
      // be mistaken for an ApplyResult. Sync returns pass through unchanged.
      const result = await handle.apply(directive.values);
      // apply may return void (custom handles) or an ApplyResult (zodFormRegistration);
      // shape-check rather than trust, since custom handles can return anything.
      if (
        result &&
        Array.isArray(result.applied) &&
        Array.isArray(result.rejected) &&
        (result.applied.length || result.rejected.length)
      ) {
        if (result.rejected.length) {
          if (deps.onApplyResult) deps.onApplyResult(directive.target, result);
          else
            console.warn(
              `[nannos] apply on ${directive.target.type}:${directive.target.id} rejected ` +
                `${result.rejected.length} field(s): ${result.rejected.map((r) => r.field).join(', ')}`,
            );
        }
        return { ok: true, applied: result.applied, rejected: result.rejected };
      }
      return { ok: true };
    }
    case 'highlight': {
      if (!deps.registry.get(directive.target.type, directive.target.id))
        return { ok: false, reason: 'unknown-target' };
      deps.highlight?.(directive.target, directive.field);
      return { ok: true };
    }
    case 'navigate': {
      deps.navigate?.(directive.to);
      return { ok: true };
    }
  }
}

/**
 * Unwrap a client-action directive from a raw `agent_response` event. Directives
 * ride status-update events tagged with `CLIENT_ACTION_EXT`, nested at
 * `status.message.parts[].data.directive` — they never appear at the top level.
 * Returns null for every other event (including streaming text chunks), so
 * callers can bail before any schema validation.
 */
export function extractClientActionDirective(data: unknown): unknown | null {
  const evt = data as {
    kind?: string;
    status?: {
      message?: {
        extensions?: string[];
        parts?: Array<{ kind?: string; data?: Record<string, unknown> }>;
      };
    };
  };
  if (evt?.kind !== 'status-update') return null;
  const msg = evt.status?.message;
  if (!msg?.extensions?.includes(CLIENT_ACTION_EXT)) return null;
  const part = msg.parts?.find((p) => p.kind === 'data' || p.data);
  return (part?.data as { directive?: unknown } | undefined)?.directive ?? null;
}
