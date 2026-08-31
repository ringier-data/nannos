/**
 * The `urn:nannos:a2a:in-task-auth:1.0` extension, Slack side.
 *
 * A tool the agent wanted to use needs the END-USER's consent (the MCP
 * gateway's `need-credentials`, surfaced as A2A's own `auth-required` task
 * state). The state is protocol; the schema of what it carries is ours, and
 * this reads it — the same precedence the Embed SDK uses (payload → metadata →
 * prose), so both clients name the same service and link the same URL.
 *
 * The status text is NOT end-user copy: it is the gateway addressing the AGENT
 * ("You must tell the end-user to…"), and the auth interrupt fires in
 * middleware, before the model, so no LLM ever rewrites it. Slack used to post
 * it verbatim. The card below is our own copy instead, and the wire text
 * survives only as the fallback for a producer that gave us no URL to link.
 */
import { Part } from '@a2a-js/sdk';

/** First http(s) URL in a text blob — the fallback when nothing structured came. */
const URL_IN_TEXT = /https?:\/\/[^\s<>"')]+/;

/**
 * Tool names that mean nothing to an end-user: sandbox and orchestration
 * plumbing. The gateway call that needs authorization often runs INSIDE one of
 * these (a `need-credentials` from an MCP call made in the sandbox is reported
 * against `eval`), so naming them misinforms rather than informs. Anything else
 * is shown verbatim — a mangled tool name is worse than a plain one.
 * Kept in step with the Embed SDK's own list (auth-required-card.tsx).
 */
const OPAQUE_TOOLS = new Set(['eval', 'task', 'client_action', 'python', 'bash', 'shell', 'call_tool']);

export interface AuthPrompt {
  /** Where the user completes the flow. Without it there is no card, only text. */
  authUrl?: string;
  /** The tool call that needed the credential, when it is fit to name. */
  tool?: string;
  /** Whose credential this is (e.g. "github"), when the producer named one. */
  service?: string;
  /** The gateway's own words — agent-facing; shown only when there is no URL. */
  message?: string;
}

/**
 * The in-task-auth DataPart (`AuthPayload.client_payload()`), when one came.
 *
 * Only the first method carrying a URL is used: the model allows several ("try
 * in order"), but a card offers one way forward, and a method without a URL is
 * nothing a Slack button can act on.
 */
function readAuthDataPart(parts: Part[] | undefined): AuthPrompt | null {
  for (const part of parts ?? []) {
    if (part.kind !== 'data') continue;
    const data = (part as { kind: 'data'; data: Record<string, unknown> }).data;
    const requirement = data?.auth_requirement;
    if (typeof requirement !== 'object' || requirement === null) continue;
    const req = requirement as { service?: unknown; resource?: unknown; auth_methods?: unknown };
    const methods = Array.isArray(req.auth_methods) ? req.auth_methods : [];
    const withUrl = methods.find(
      (m) => typeof (m as { auth_url?: unknown })?.auth_url === 'string' && (m as { auth_url: string }).auth_url
    ) as { auth_url?: string } | undefined;
    return {
      ...(withUrl?.auth_url && { authUrl: withUrl.auth_url }),
      // `resource` is the specific thing that needed the credential (the tool
      // call); `service` is who it belongs to. The card names them differently.
      ...(typeof req.resource === 'string' && req.resource && { tool: req.resource }),
      ...(typeof req.service === 'string' && req.service && { service: req.service }),
    };
  }
  return null;
}

/**
 * The authorize target for an `auth-required` status, in descending order of how
 * much the producer told us: the structured DataPart of the in-task-auth
 * extension, then the status metadata (`auth_url` + the `tool` that asked), then
 * — last resort — the first URL in the message text.
 *
 * That last resort scrapes a sentence written for the agent, and it stays only
 * because a stranded user is worse than an ugly parse: producers that negotiated
 * the extension never reach it.
 */
export function readAuthRequired(parts: Part[] | undefined, metadata?: Record<string, unknown>): AuthPrompt {
  const text = (parts ?? [])
    .filter((p) => p.kind === 'text')
    .map((p) => (p as { kind: 'text'; text: string }).text)
    .join('\n')
    .trim();
  const structured = readAuthDataPart(parts);
  const fromMeta = metadata?.auth_url ?? metadata?.authUrl ?? metadata?.authorizeUrl;
  const authUrl =
    structured?.authUrl ??
    (typeof fromMeta === 'string' && fromMeta ? fromMeta : (text.match(URL_IN_TEXT)?.[0] ?? undefined));
  const rawTool = structured?.tool ?? (typeof metadata?.tool === 'string' ? metadata.tool : undefined);
  const tool = rawTool && !OPAQUE_TOOLS.has(rawTool) ? rawTool : undefined;
  // The same filter applies to the service: a producer filling it in from the
  // tool name would otherwise slip `eval` past the check that exists precisely
  // to keep sandbox plumbing out of the user's face.
  const service = structured?.service && !OPAQUE_TOOLS.has(structured.service) ? structured.service : undefined;
  return {
    ...(authUrl && { authUrl }),
    ...(tool && { tool }),
    ...(service && { service }),
    ...(text && { message: text }),
  };
}

/** What to call the thing being authorized — nothing, when neither is fit to show. */
export function authSubject(prompt: AuthPrompt): string {
  return prompt.service || prompt.tool || '';
}

/**
 * What the buttons send as text. Agent-facing, so it is the same English the
 * Embed SDK sends — the fallback for a server that never routed the DataPart,
 * while the DataPart beside it is what the middleware acts on when it did.
 *
 * On that fallback path the words are READ, not just logged: a fast-LLM
 * classifier grades them approve / reject / unclear before the agent ever sees
 * them (orchestrator ``AuthErrorDetectionMiddleware._after_auth_interrupt`` ->
 * ``classify_reply``), and an unclear verdict costs the user a whole extra
 * round. So keep both sentences blunt and unambiguous about the DECISION —
 * softening them ("maybe later", "I'll get to it") is what makes them
 * unclassifiable.
 */
export function authResumeText(decision: 'approved' | 'declined', tool?: string): string {
  if (decision === 'approved') {
    return tool
      ? `I have completed the authorization for ${tool}. Please retry what needed it and continue.`
      : 'I have completed the authorization. Please retry what needed it and continue.';
  }
  return tool
    ? `I am not going to authorize ${tool}. Do not ask again — tell me what you cannot do without it.`
    : 'I am not going to authorize this. Do not ask again — tell me what you cannot do without it.';
}

/** The DataPart the executor routes straight to the parked auth interrupt. */
export function authorizationDataPart(decision: 'approved' | 'declined'): Record<string, unknown> {
  return { authorization: { decision } };
}

export const AUTH_ACTION_OPEN = 'nannos_auth_open';
export const AUTH_ACTION_DONE = 'nannos_auth_done';
export const AUTH_ACTION_DECLINE = 'nannos_auth_decline';

export interface AuthWidgetData extends AuthPrompt {
  taskId: string;
  contextId: string;
  channelId: string;
  threadTs: string;
  /** Existing plan-widget ts, carried through the resume. */
  planMessageTs?: string;
  /** Open thinking-steps stream ts, so the resume continues the same widget. */
  streamMessageTs?: string;
}

/**
 * The authorization card: our copy, one link out and one answer back.
 *
 * Unlike the panel this does NOT walk through stages (Authorize, then "Done,
 * continue"). Slack could — a link button fires an action too — but that makes
 * the way forward depend on a `chat.update` succeeding, and a failed update
 * would leave the user looking at a card with no way to confirm. All three
 * buttons are therefore live from the start; on a chat surface a stable card
 * reads better than a reactive one anyway.
 */
export function buildAuthRequiredWidget(data: AuthWidgetData): any[] {
  const subject = authSubject(data);
  const payload = {
    taskId: data.taskId,
    contextId: data.contextId,
    channelId: data.channelId,
    threadTs: data.threadTs,
    subject,
    ...(data.tool ? { tool: data.tool } : {}),
    ...(data.planMessageTs ? { planMessageTs: data.planMessageTs } : {}),
    ...(data.streamMessageTs ? { streamMessageTs: data.streamMessageTs } : {}),
  };
  const encodedData = Buffer.from(JSON.stringify(payload)).toString('base64');

  const blocks: any[] = [
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: [
          `*🔐 ${subject ? `Authorization needed for ${subject}` : 'Authorization needed'}*`,
          data.tool
            ? `Nannos needs your permission to use \`${data.tool}\`.`
            : 'Nannos needs your permission before it can continue.',
        ].join('\n'),
      },
    },
  ];

  if (!data.authUrl) {
    // No URL anywhere: the gateway's own text is all the user has to go on, so
    // show it rather than an empty card. Answering is still offered — the user
    // may be able to authorize by another route, and declining still needs to
    // reach the agent.
    if (data.message) {
      blocks.push({ type: 'section', text: { type: 'mrkdwn', text: data.message.substring(0, 2900) } });
    }
  } else {
    blocks.push({
      type: 'context',
      elements: [
        { type: 'mrkdwn', text: 'Authorize in the browser, then come back and confirm here.' },
      ],
    });
  }

  const elements: any[] = [];
  if (data.authUrl) {
    elements.push({
      type: 'button',
      text: { type: 'plain_text', text: 'Authorize' },
      // A URL button opens the browser; the action_id exists so Slack gets its
      // ack (without one it warns "operation timeout" after 3s).
      url: data.authUrl,
      action_id: AUTH_ACTION_OPEN,
      value: encodedData,
      style: 'primary',
    });
  }
  elements.push({
    type: 'button',
    text: { type: 'plain_text', text: 'Done, continue' },
    action_id: AUTH_ACTION_DONE,
    value: encodedData,
  });
  elements.push({
    type: 'button',
    text: { type: 'plain_text', text: "Don't allow" },
    action_id: AUTH_ACTION_DECLINE,
    value: encodedData,
    style: 'danger',
  });
  blocks.push({ type: 'actions', elements });

  return blocks;
}
