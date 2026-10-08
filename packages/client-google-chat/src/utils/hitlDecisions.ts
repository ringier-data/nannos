/**
 * Approval-card call ids, and the decisions a click sends with them.
 *
 * Mirrors client-slack: a card names the calls it answers, so a click on a card that
 * was already answered in words, or replaced by a newer card, cannot answer anything
 * else — the server refuses it instead of running a turn.
 */

/** Args the server adds for itself (`_call_id`, `_summary`, `_risk_metadata`, …), never the call's own. */
export function isInternalArg(key: string): boolean {
  return key.startsWith('_');
}

/** The call ids of a card's pending calls, in order; calls without one are skipped. */
export function callIdsOf(actionRequests: any[] | undefined): string[] {
  return (actionRequests || [])
    .map((a) => a?.args?._call_id)
    .filter((id): id is string => typeof id === 'string' && id.length > 0);
}

/**
 * One decision per call the clicked card names, each carrying its call id. Cards from
 * before call ids were carried send the bare decision.
 */
export function forCalls<T extends { type: string }>(decision: T, callIds: unknown): Array<T & { id?: string }> {
  const ids = Array.isArray(callIds) ? callIds.filter((id): id is string => typeof id === 'string' && id.length > 0) : [];
  return ids.length > 0 ? ids.map((id) => ({ ...decision, id })) : [decision];
}

/** One call's typed answer, as the server read it (hitl-decision extension). */
export interface TypedDecision {
  id?: string;
  type?: string;
  intent?: string;
}

/** What the card says once a typed answer settled it. */
export function typedDecisionText(decision: TypedDecision): string {
  if (decision.type === 'approve') return '✅ Approved in your reply';
  if (decision.intent === 'reject') return '❌ Rejected in your reply';
  if (decision.intent === 'change') return '✏️ Changes requested in your reply';
  return 'ℹ️ Not run — your reply was not an answer to this request';
}
