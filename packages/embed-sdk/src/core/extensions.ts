/**
 * A2A extension URIs — the shared vocabulary for classifying streaming events.
 * Both the Embed SDK and console-frontend agree on these ids. Each is rendered
 * by a "styleable client-side renderer" (ADR-0003); `client-action` is the new
 * agent→widget return channel that carries in-form `apply` / `highlight` /
 * `navigate` directives.
 */
export const ACTIVITY_LOG_EXT = 'urn:nannos:a2a:activity-log:1.0';
export const WORK_PLAN_EXT = 'urn:nannos:a2a:work-plan:1.0';
export const INTERMEDIATE_OUTPUT_EXT = 'urn:nannos:a2a:intermediate-output:1.0';
export const FEEDBACK_REQUEST_EXT = 'urn:nannos:a2a:feedback-request:1.0';
/** Tool approvals. The request's `action_requests[].args._call_id` identifies ONE
 *  ASK — unique per question, NOT per (tool, args): the same call asked about twice
 *  carries two different ids. Echo it back as the decision's `id`, and key any
 *  "already answered" suppression on it; do not derive an identity from the tool
 *  name and args, or a genuine second ask reads as a replay and its card is dropped.
 *  Full payload contract: orchestrator `app/core/a2a_extensions.py`. */
export const HITL_EXT = 'urn:nannos:a2a:human-in-the-loop:1.0';
export const CONVERSATION_ORIGIN_EXT = 'urn:nannos:a2a:conversation-origin:1.0';
export const CLIENT_ACTION_EXT = 'urn:nannos:a2a:client-action:1.0';
/** Structured auth payload on A2A's own `auth-required` state (agent-common
 *  in_task_auth.py). The STATE is protocol; only the DataPart's schema is ours,
 *  so this extension ADDS a part rather than replacing the state — a client that
 *  does not negotiate it still gets the text part and behaves as before. */
export const IN_TASK_AUTH_EXT = 'urn:nannos:a2a:in-task-auth:1.0';

/**
 * Message-metadata `kind` discriminators of the activity-log extension. No kind
 * = a mechanical line (a tool ran); `note` = the agent's own words for the user,
 * emitted mid-turn by the `notify_user` tool. Pinned to a2a-extensions.json.
 */
export const ACTIVITY_LOG_KINDS = ['note'] as const;
export type ActivityLogKind = (typeof ACTIVITY_LOG_KINDS)[number];

/** All extension ids supported by the Embed SDK runtime. */
export const SUPPORTED_EXTENSIONS = [
  ACTIVITY_LOG_EXT,
  WORK_PLAN_EXT,
  INTERMEDIATE_OUTPUT_EXT,
  FEEDBACK_REQUEST_EXT,
  HITL_EXT,
  CONVERSATION_ORIGIN_EXT,
  CLIENT_ACTION_EXT,
  IN_TASK_AUTH_EXT,
] as const;

/** Value for the `X-A2A-Extensions` negotiation header. */
export const X_A2A_EXTENSIONS_HEADER = SUPPORTED_EXTENSIONS.join(', ');
