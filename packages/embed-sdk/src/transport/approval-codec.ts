/**
 * Codec between the backend's HITL `Decision` shape and the AI SDK's native
 * tool-approval response (`{ approved: boolean, reason?: string }`).
 *
 * The AI SDK models exactly approve/reject; the Nannos backend also knows
 * `edit` (request changes with a message) and bypass flags. Those extras ride
 * the `reason` field as a small versioned JSON envelope. This file is the ONLY
 * place that reads or writes that envelope — everything else sees `Decision`
 * on one side and `{approved, reason}` on the other. A malformed envelope
 * degrades to a plain approve/reject (never a dropped decision).
 */
import { z } from 'zod';

/** A single HITL decision as the backend consumes it (aligned by `id` = `_call_id`). */
export interface Decision {
  id?: string;
  type: 'approve' | 'reject' | 'edit';
  message?: string;
  bypass?: boolean;
  bypass_all?: boolean;
  bypass_pattern?: string | null;
  /** Client-action round trip: the browser's execution result, resumed into the
   *  paused `client_action` tool (executor matches it to the interrupt by `id`). */
  client_action_result?: Record<string, unknown>;
}

/** What `addToolApprovalResponse` carries / what the transport reads back off the part. */
export interface ApprovalResponse {
  approved: boolean;
  reason?: string;
}

const envelopeSchema = z.object({
  v: z.literal(1),
  type: z.enum(['approve', 'reject', 'edit']).optional(),
  message: z.string().optional(),
  bypass: z.boolean().optional(),
  bypass_all: z.boolean().optional(),
  bypass_pattern: z.string().nullable().optional(),
  clientActionResult: z.record(z.string(), z.unknown()).optional(),
});

type Envelope = z.infer<typeof envelopeSchema>;

/** Encode a Decision into the AI SDK approval-response shape. */
export function encodeApproval(decision: Decision): ApprovalResponse {
  if (decision.type === 'approve') {
    const hasBypass = decision.bypass || decision.bypass_all || decision.bypass_pattern != null;
    if (hasBypass || decision.client_action_result) {
      const envelope: Envelope = {
        v: 1,
        ...(hasBypass && { bypass: decision.bypass ?? true }),
        ...(decision.bypass_all !== undefined && { bypass_all: decision.bypass_all }),
        ...(decision.bypass_pattern !== undefined && { bypass_pattern: decision.bypass_pattern }),
        ...(decision.client_action_result && { clientActionResult: decision.client_action_result }),
      };
      return { approved: true, reason: JSON.stringify(envelope) };
    }
    return { approved: true };
  }
  // reject / edit
  const envelope: Envelope = {
    v: 1,
    type: decision.type,
    ...(decision.message !== undefined && { message: decision.message }),
  };
  return { approved: false, reason: JSON.stringify(envelope) };
}

/** Decode an approval response (from an `approval-responded` tool part) back into a Decision. */
export function decodeApproval(approvalId: string, response: ApprovalResponse): Decision {
  const base: Decision = { id: approvalId, type: response.approved ? 'approve' : 'reject' };
  if (!response.reason) return base;

  let parsed: unknown;
  try {
    parsed = JSON.parse(response.reason);
  } catch {
    // A plain human string (not our envelope): treat it as the decision message.
    return { ...base, message: response.reason };
  }
  const envelope = envelopeSchema.safeParse(parsed);
  if (!envelope.success) {
    return { ...base, message: response.reason };
  }
  const e = envelope.data;
  if (response.approved) {
    return {
      ...base,
      ...(e.bypass !== undefined && { bypass: e.bypass }),
      ...(e.bypass_all !== undefined && { bypass_all: e.bypass_all }),
      ...(e.bypass_pattern !== undefined && { bypass_pattern: e.bypass_pattern }),
      ...(e.clientActionResult && { client_action_result: e.clientActionResult }),
    };
  }
  return {
    ...base,
    type: e.type === 'edit' ? 'edit' : 'reject',
    ...(e.message !== undefined && { message: e.message }),
  };
}
