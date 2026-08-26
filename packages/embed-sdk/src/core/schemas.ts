import { z } from 'zod';

/**
 * Zod schemas for the `urn:nannos:a2a:client-action` extension payloads. The core
 * validates every inbound directive HERE, at the boundary, before touching a host
 * handle — an untrusted/garbled directive must never reach `apply`.
 *
 * The `kind` literals are the canonical vocabulary shared with the agent-side tool
 * schema; `schemas.test.ts` pins them to `a2a-extensions.json` so the two cannot
 * drift (a kind the agent can emit but this union refuses is a silent no-op the
 * user is told succeeded).
 */

export const applyDirective = z.object({
  kind: z.literal('apply'),
  target: z.object({ type: z.string(), id: z.string() }),
  values: z.record(z.string(), z.unknown()),
  /** Accepted for wire compatibility and then IGNORED — do not treat it as a gate.
   *  Approval happens once upstream, at the agent's tool-call HITL gate (`apply`
   *  is risk-scored to always interrupt), so a directive that reaches the SDK is
   *  already approved and a second confirm would double-prompt. See the contract
   *  note on `executeClientAction`. */
  confirm: z.boolean().optional(),
});

export const highlightDirective = z.object({
  kind: z.literal('highlight'),
  target: z.object({ type: z.string(), id: z.string() }),
  field: z.string().optional(),
});

export const navigateDirective = z.object({
  kind: z.literal('navigate'),
  to: z.string(),
});

/** The awaited pull: the agent asks what the user currently sees. Answered from
 *  the merged page context + host-registered readers, through `sanitizeReadResult`. */
export const readCurrentPageDirective = z.object({
  kind: z.literal('read_current_page'),
});

export const clientActionDirective = z.discriminatedUnion('kind', [
  applyDirective,
  highlightDirective,
  navigateDirective,
  readCurrentPageDirective,
]);

export type ClientActionDirective = z.infer<typeof clientActionDirective>;
