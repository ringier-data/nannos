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
export const HITL_EXT = 'urn:nannos:a2a:human-in-the-loop:1.0';
export const CLIENT_ACTION_EXT = 'urn:nannos:a2a:client-action:1.0';

/** All extension ids supported by the Embed SDK runtime. */
export const SUPPORTED_EXTENSIONS = [
  ACTIVITY_LOG_EXT,
  WORK_PLAN_EXT,
  INTERMEDIATE_OUTPUT_EXT,
  FEEDBACK_REQUEST_EXT,
  HITL_EXT,
  CLIENT_ACTION_EXT,
] as const;

/** Value for the `X-A2A-Extensions` negotiation header. */
export const X_A2A_EXTENSIONS_HEADER = SUPPORTED_EXTENSIONS.join(', ');
