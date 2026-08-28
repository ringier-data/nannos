/**
 * Persisted REST rows → `NannosUIMessage[]`.
 *
 * The backend stores one row per protocol event (user messages, streamed
 * finals, activity-log statuses, intermediate-output artifacts, task rows...).
 * The mapper folds them into turns the way the live demux renders them: a user
 * row starts a group; every agent row until the next user row contributes
 * PARTS to one assistant message (activity lines, sub-agent thoughts, text).
 * Ported from the retired ChatContext loadMessages/reconstructTimeline
 * (ChatContext.tsx:1125-1334 & :143-245 at tag embed-sdk-v1).
 *
 * Also restores a pending HITL interrupt: when the newest rows end in an
 * unresolved `input-required` + HITL row, the last assistant message gets the
 * same `approval-requested` dynamic-tool parts the live path emits — so
 * `addToolApprovalResponse` + the auto-send resume work identically after a
 * reload.
 */
import {
  extractPartTexts,
  getFileInfo,
  getPartKind,
  getTaskState,
  shouldDisplayMessageParts,
} from '../core/protocol';
import { ACTIVITY_LOG_EXT, CLIENT_ACTION_EXT, HITL_EXT, INTERMEDIATE_OUTPUT_EXT } from '../core/extensions';
import { clientActionPartId } from './approval-codec';
import { textArrival } from './ai-types';
import type { NannosMessageMetadata, NannosUIMessage, ReviewConfig } from './ai-types';
import { readAuthRequired } from './demux';
import { labelAgentEvent, serverWireId } from './wire-log';

/** The persisted message row as the REST API returns it (tolerant shape). */
export interface RestMessageRow {
  id?: string;
  message_id?: string;
  messageId?: string;
  role?: string;
  user_id?: string | null;
  content?: unknown;
  parts?: unknown;
  kind?: string;
  /** A2A TaskState: protobuf INT from the REST endpoint; strings accepted for
   *  older rows and tests. */
  state?: string | number;
  created_at?: string;
  timestamp?: string;
  sort_key?: string;
  metadata?: Record<string, unknown> | null;
  raw_payload?: string | null;
  [key: string]: unknown;
}

type Part = NannosUIMessage['parts'][number];

function rowTime(row: RestMessageRow): number {
  const ts = row.created_at ?? row.timestamp ?? row.sort_key;
  const t = ts ? new Date(ts).getTime() : NaN;
  return Number.isNaN(t) ? 0 : t;
}

function rowId(row: RestMessageRow, fallback: string): string {
  return (row.id ?? row.message_id ?? row.messageId ?? fallback) as string;
}

function parsePayload(row: RestMessageRow): Record<string, unknown> | null {
  if (typeof row.raw_payload !== 'string' || !row.raw_payload) return null;
  try {
    return JSON.parse(row.raw_payload) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function rowText(row: RestMessageRow): string {
  if (typeof row.content === 'string' && row.content) return row.content;
  if (Array.isArray(row.parts)) return extractPartTexts(row.parts as never).join('\n');
  if (typeof row.parts === 'string') return row.parts;
  return '';
}

interface PayloadFacts {
  statusExtensions: string[];
  statusMessage: Record<string, unknown> | undefined;
  /** Payload-level metadata (where the orchestrator puts `auth_url` / `tool`). */
  topMeta: Record<string, unknown> | undefined;
  artifactExtensions: string[];
  artifactMetadata: Record<string, unknown> | undefined;
  artifactParts: Array<{ kind?: string; text?: string }> | undefined;
  source?: string;
  /** `'note'` for a mid-turn note (notify_user); undefined for a machine line. */
  noteKind?: 'note';
}

function payloadFacts(payload: Record<string, unknown> | null): PayloadFacts {
  const status = payload?.status as Record<string, unknown> | undefined;
  const statusMessage = status?.message as Record<string, unknown> | undefined;
  const artifact = payload?.artifact as Record<string, unknown> | undefined;
  const msgMeta = statusMessage?.metadata as Record<string, unknown> | undefined;
  const topMeta = payload?.metadata as Record<string, unknown> | undefined;
  const source =
    typeof msgMeta?.source === 'string'
      ? msgMeta.source
      : typeof topMeta?.source === 'string'
        ? topMeta.source
        : undefined;
  return {
    statusExtensions: (statusMessage?.extensions ?? []) as string[],
    statusMessage,
    topMeta,
    artifactExtensions: ((artifact?.extensions ?? []) as string[]) || [],
    artifactMetadata: artifact?.metadata as Record<string, unknown> | undefined,
    artifactParts: artifact?.parts as Array<{ kind?: string; text?: string }> | undefined,
    source,
    noteKind: msgMeta?.kind === 'note' ? 'note' : undefined,
  };
}

/**
 * How a panel-composed user row renders on restore. `context` is the default —
 * the host-injected chip this metadata was introduced for — but a decision made
 * in an interrupt card was persisted as a `receipt` (with its outcome), and
 * reloading it as a chip turned "Authorized GitHub · asked Nannos to retry" into
 * "Context: Authorized GitHub" over the agent-facing prompt.
 */
function injectedDisplay(
  row: RestMessageRow,
  label: string,
): NonNullable<NannosMessageMetadata['display']> {
  const kind = row.metadata?.injectedDisplayKind === 'receipt' ? 'receipt' : 'context';
  const persisted = row.metadata?.injectedDisplayOutcome;
  const outcome =
    persisted === 'skipped' ? ('skipped' as const) : persisted === 'authorized' ? ('authorized' as const) : undefined;
  return { kind, label, ...(kind === 'receipt' && outcome ? { outcome } : {}) };
}

/** Build the user message for a `role === 'user'` row. */
function userMessage(row: RestMessageRow, index: number): NannosUIMessage {
  const parts: Part[] = [];
  const text = rowText(row);
  if (text) parts.push({ type: 'text', text });
  if (Array.isArray(row.parts)) {
    for (const p of row.parts as unknown[]) {
      const file = getFileInfo(p);
      if (file) {
        parts.push({
          type: 'file',
          url: file.uri,
          mediaType: file.mimeType ?? 'application/octet-stream',
          filename: file.name,
        });
      }
    }
  }
  const injectedDisplayText = row.metadata?.injectedDisplayText;
  return {
    id: rowId(row, `hist-u-${index}`),
    role: 'user',
    parts,
    ...(typeof injectedDisplayText === 'string' && injectedDisplayText
      ? { metadata: { display: injectedDisplay(row, injectedDisplayText) } }
      : {}),
  };
}

/**
 * Map one page of rows (any order; sorted internally by time) into UI
 * messages. Emits complete assistant turns — the caller prepends/replaces via
 * `chat.setMessages` and dedupes on message ids (`persistedMessageId` is set on
 * every assistant message so live-finalized turns reconcile with refetches).
 */
export function rowsToUIMessages(rows: RestMessageRow[]): NannosUIMessage[] {
  const sorted = [...rows].sort((a, b) => rowTime(a) - rowTime(b));
  const messages: NannosUIMessage[] = [];

  let assistantParts: Part[] = [];
  let assistantId: string | null = null;
  let seq = 0;

  const flushAssistant = () => {
    if (assistantParts.length === 0) {
      assistantId = null;
      return;
    }
    const id = assistantId ?? `hist-a-${messages.length}`;
    messages.push({
      id,
      role: 'assistant',
      parts: assistantParts,
      metadata: { persistedMessageId: id },
    });
    assistantParts = [];
    assistantId = null;
  };

  for (const [index, row] of sorted.entries()) {
    const role = row.role ?? (row.user_id ? 'user' : 'agent');
    if (role === 'user') {
      const message = userMessage(row, index);
      // A HITL RESUME is persisted as a user row with an EMPTY message: the
      // decisions (or a client-action result) ride `dataParts`, never text. It
      // is nothing the user said and has nothing to show, so it renders no
      // bubble AND does not break the turn — the agent parts on either side of
      // the approval belong to one assistant message, exactly as the live path
      // streams them.
      if (message.parts.length === 0) continue;
      flushAssistant();
      messages.push(message);
      continue;
    }

    const payload = parsePayload(row);
    const facts = payloadFacts(payload);
    const time = rowTime(row) || Date.now();
    seq += 1;

    // Dev-mode provenance, same contract as the live demux: the wire label of
    // the stored event, and the row's SERVER id — the same id the wire replay
    // stamps on its entries (`serverWireId`), so the badge resolves the raw
    // event exactly once the backend record is loaded.
    const wire = payload ? labelAgentEvent(payload) : undefined;
    const wireId = serverWireId(row);
    const provenance = { ...(wire && { wire }), ...(wireId && { wireId }) };

    // Sub-agent thought (intermediate-output artifact).
    if (row.kind === 'artifact-update' && facts.artifactExtensions.includes(INTERMEDIATE_OUTPUT_EXT)) {
      const agent = (facts.artifactMetadata?.agent_name as string) || 'sub-agent';
      const text =
        (facts.artifactParts ? extractPartTexts(facts.artifactParts).join('') : rowText(row)).trim();
      if (text) {
        assistantParts.push({
          type: 'data-agent-thought',
          id: `hist-thought-${seq}`,
          data: { agent, text, complete: true, startedAt: time, ...provenance },
        });
      }
      continue;
    }

    // Activity-log line.
    if (facts.statusExtensions.includes(ACTIVITY_LOG_EXT)) {
      let text = '';
      const nested = facts.statusMessage;
      if (typeof nested?.parts !== 'undefined' && Array.isArray(nested.parts)) {
        text = extractPartTexts(nested.parts as never).join(' ').trim();
      }
      if (!text) text = rowText(row).trim();
      if (text) {
        assistantParts.push({
          type: 'data-activity',
          id: `hist-act-${seq}`,
          data: {
            text,
            ...(facts.source && { source: facts.source }),
            ...(facts.noteKind && { kind: facts.noteKind }),
            ts: time,
            ...provenance,
          },
        });
      }
      continue;
    }

    // Working-state progress line (no extension) → activity.
    const state = getTaskState(row.state);
    if (row.kind === 'status-update' && state === 'working') {
      const text = rowText(row).trim();
      if (text) {
        assistantParts.push({
          type: 'data-activity',
          id: `hist-act-${seq}`,
          data: { text, ts: time, ...provenance },
        });
      }
      continue;
    }

    // Secondary-authorization prompt → the SAME structured part the live demux
    // emits, so a reload keeps the localized card instead of falling through to
    // the text branch below and printing the gateway's agent-directed message.
    if (row.kind === 'status-update' && state === 'auth-required') {
      const nested = facts.statusMessage;
      const text = Array.isArray(nested?.parts)
        ? extractPartTexts(nested.parts as never).join('\n')
        : rowText(row);
      assistantParts.push({
        type: 'data-auth-required',
        id: `hist-auth-${seq}`,
        data: {
          ...readAuthRequired(
            text,
            facts.topMeta ?? (nested?.metadata as Record<string, unknown> | undefined),
            nested?.parts as Array<Record<string, unknown>> | undefined,
          ),
          ...provenance,
        },
      });
      continue;
    }

    // Approval prompt (HITL risk gate / client-action round trip) → never text.
    // Its status text is the gate's note to the agent ("Tool 'client_action'
    // has risk score 0.90 (threshold: 0.80)") — the user reads the approval
    // card instead, which `findPendingInterrupt` restores while the prompt is
    // still open. An ANSWERED prompt leaves no trace at all, exactly as the
    // live demux renders it. Plain `input-required` rows (no extension) are a
    // real question to the user and still fall through to the text branch.
    if (
      row.kind === 'status-update' &&
      state === 'input-required' &&
      (facts.statusExtensions.includes(HITL_EXT) ||
        facts.statusExtensions.includes(CLIENT_ACTION_EXT))
    ) {
      continue;
    }

    // Protocol task rows never render.
    if (row.kind === 'task') continue;

    // Files the AGENT produced (a generated report, an image) ride the row's
    // parts next to its text. They are the turn's deliverable, so they stay
    // with it — as `file` parts the thread renders as download links.
    if (Array.isArray(row.parts)) {
      for (const p of row.parts as unknown[]) {
        const file = getFileInfo(p);
        if (!file) continue;
        const duplicate = assistantParts.some(
          (part) => part.type === 'file' && part.url === file.uri,
        );
        if (duplicate) continue;
        assistantParts.push({
          type: 'file',
          url: file.uri,
          mediaType: file.mimeType ?? 'application/octet-stream',
          filename: file.name,
        });
        assistantId = rowId(row, `hist-a-${index}`);
      }
    }

    // Displayable agent text → the turn's text part; the row's id becomes the
    // assistant message id (matches the live path, which finalizes under the
    // persisted DB id).
    const text = rowText(row);
    const displayable = Array.isArray(row.parts)
      ? shouldDisplayMessageParts(row.parts as never) || !!text.trim()
      : !!text.trim();
    if (displayable && text.trim()) {
      // ONE answer, persisted several times: the streamed final (artifact
      // row), the full agent message, and a terminal status can all carry the
      // same text, each under its own row id. Live, `emitAuthoritativeText`
      // reconciles them into one part; mirror that here — a repeat is
      // dropped (the first row keeps naming the source, as the live path
      // keeps the streamed part), an EXTENDING text supersedes in place.
      const lastText = assistantParts
        .filter((p): p is Extract<Part, { type: 'text' }> => p.type === 'text')
        .pop();
      const prev = lastText?.text.trim();
      const next = text.trim();
      if (prev !== undefined && lastText && prev.startsWith(next)) {
        // repeat (equal or shorter): nothing new to show
      } else if (prev !== undefined && lastText && next.startsWith(prev)) {
        lastText.text = text;
        lastText.providerMetadata = textArrival(time, wire, wireId);
      } else {
        assistantParts.push({
          type: 'text',
          text,
          providerMetadata: textArrival(time, wire, wireId),
        });
      }
      // Every displayable row still names the turn — the live path finalizes
      // under the LAST persisted DB id, repeats included.
      assistantId = rowId(row, `hist-a-${index}`);
    }
  }
  flushAssistant();
  return messages;
}

export interface RestoredInterrupt {
  reason: string;
  actionRequests: Array<{ name: string; args: Record<string, unknown>; description?: string }>;
  reviewConfigs: ReviewConfig[];
}

/**
 * Detect an UNRESOLVED pending interrupt in a page of rows: the most recent
 * `input-required` row carrying either the HITL extension (human approval) or
 * a client-action REQUEST (`{request}` payload — the awaited round trip), with
 * no later non-input-required status. Both restore into the same
 * approval-shaped parts; the client-action one is marked
 * `_clientActionRequest`, so useNannosChat re-executes and auto-resumes it
 * instead of rendering a card. (ChatContext.tsx:1278-1328 semantics.)
 */
export function findPendingInterrupt(rows: RestMessageRow[]): RestoredInterrupt | null {
  const requestOf = (
    payload: Record<string, unknown> | null,
  ): { id?: string; directive?: unknown } | null => {
    const parts = (payloadFacts(payload).statusMessage?.parts ?? []) as Array<Record<string, unknown>>;
    for (const part of parts) {
      if (getPartKind(part) !== 'data') continue;
      const request = (part.data as { request?: { id?: string; directive?: unknown } } | undefined)
        ?.request;
      if (request?.id && request.directive) return request;
    }
    return null;
  };

  const interruptRow = [...rows].reverse().find((row) => {
    if (row.kind !== 'status-update' || getTaskState(row.state) !== 'input-required') return false;
    const payload = parsePayload(row);
    const exts = payloadFacts(payload).statusExtensions;
    if (exts.includes(HITL_EXT)) return true;
    return exts.includes(CLIENT_ACTION_EXT) && requestOf(payload) !== null;
  });
  if (!interruptRow) return null;

  const interruptTime = rowTime(interruptRow);
  const resolved = rows.some((row) => {
    if (row.kind !== 'status-update' || getTaskState(row.state) === 'input-required') return false;
    return rowTime(row) > interruptTime;
  });
  if (resolved) return null;

  const payload = parsePayload(interruptRow);
  const facts = payloadFacts(payload);

  if (facts.statusExtensions.includes(CLIENT_ACTION_EXT)) {
    const request = requestOf(payload)!;
    return {
      reason: '',
      actionRequests: [
        {
          name: 'client_action',
          args: {
            directive: request.directive,
            _clientActionRequest: true,
            // Same derived part id the live path uses, so a reload restores the
            // request as its own part even when the risk gate's approval for
            // that call id is also in the mapped history.
            _call_id: clientActionPartId(request.id!),
          },
        },
      ],
      reviewConfigs: [],
    };
  }

  const parts = (facts.statusMessage?.parts ?? []) as Array<Record<string, unknown>>;
  const result: RestoredInterrupt = { reason: '', actionRequests: [], reviewConfigs: [] };
  for (const part of parts) {
    const kind = getPartKind(part);
    if (kind === 'data') {
      const d = part.data as Record<string, unknown> | undefined;
      if (Array.isArray(d?.action_requests)) {
        result.actionRequests = d.action_requests as RestoredInterrupt['actionRequests'];
      }
      if (Array.isArray(d?.review_configs)) {
        result.reviewConfigs = d.review_configs as ReviewConfig[];
      }
    } else if (kind === 'text') {
      result.reason = (part.text as string) || '';
    }
  }
  return result.actionRequests.length > 0 ? result : null;
}

/**
 * Append the restored interrupt to the mapped messages as live-identical
 * `approval-requested` dynamic-tool parts (on the last assistant message, or a
 * synthetic one when the interrupt is the newest thing in the conversation).
 */
export function appendRestoredInterrupt(
  messages: NannosUIMessage[],
  interrupt: RestoredInterrupt,
): NannosUIMessage[] {
  const firstAction = interrupt.actionRequests[0];
  const hitlMeta = {
    reason:
      (firstAction?.args?.description as string) ||
      (firstAction?.args?.reason as string) ||
      interrupt.reason,
    reviewConfigs: interrupt.reviewConfigs,
  };
  const toolParts: Part[] = interrupt.actionRequests.map((action, i) => {
    const callId = (action.args?._call_id as string) || `restored-${i}`;
    return {
      type: 'dynamic-tool',
      toolName: action.name,
      toolCallId: callId,
      state: 'approval-requested',
      input: action.args ?? {},
      approval: { id: callId },
    } as Part;
  });

  const last = messages[messages.length - 1];
  if (last?.role === 'assistant') {
    return [
      ...messages.slice(0, -1),
      { ...last, parts: [...last.parts, ...toolParts], metadata: { ...last.metadata, hitl: hitlMeta } },
    ];
  }
  return [
    ...messages,
    { id: 'restored-interrupt', role: 'assistant', parts: toolParts, metadata: { hitl: hitlMeta } },
  ];
}
