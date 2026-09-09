/**
 * The one rule for a sub-agent's name, shared by every form that collects one.
 *
 * A name is not only a label: the orchestrator puts it in its task tool's enum, so it has
 * to be a plain identifier. `SUB_AGENT_NAME_RE` in console-backend's `models/sub_agent.py`
 * is the authority; this mirrors it so the user is told before the request, not by a 422.
 *
 * Keep the two in step. A name the backend rejects but a form accepts used to survive all
 * the way to chat time, where it broke the owner's every turn — not just that agent.
 */
export const SUB_AGENT_NAME_RE = /^[a-zA-Z][a-zA-Z0-9_-]*$/;
export const SUB_AGENT_NAME_MAX = 64;

/** The hint shown under a name field. */
export const SUB_AGENT_NAME_HINT =
  'Must start with a letter and use only letters, numbers, hyphens and underscores — no spaces';

/** The problem with `name`, or null when it is acceptable. */
export function subAgentNameError(name: string, label = 'Name'): string | null {
  const trimmed = name.trim();
  if (!trimmed) return `${label} is required`;
  if (trimmed.length > SUB_AGENT_NAME_MAX)
    return `${label} must be ${SUB_AGENT_NAME_MAX} characters or less`;
  if (!SUB_AGENT_NAME_RE.test(trimmed)) return `${label}: ${SUB_AGENT_NAME_HINT}`;
  return null;
}
