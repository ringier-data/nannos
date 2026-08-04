import { z } from 'zod';

/**
 * Zod schemas for the `urn:nannos:a2a:client-action` extension payloads. The core
 * validates every inbound directive HERE, at the boundary, before touching a host
 * handle — an untrusted/garbled directive must never reach `apply`.
 */

export const applyDirective = z.object({
  kind: z.literal('apply'),
  target: z.object({ type: z.string(), id: z.string() }),
  values: z.record(z.string(), z.unknown()),
  /** If true, the widget must get explicit human confirmation before applying (HITL composes on top). */
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

export const clientActionDirective = z.discriminatedUnion('kind', [
  applyDirective,
  highlightDirective,
  navigateDirective,
]);

export type ClientActionDirective = z.infer<typeof clientActionDirective>;
