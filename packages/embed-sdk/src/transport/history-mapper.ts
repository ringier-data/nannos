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
 * Approval prompts (HITL risk gate, client-action round trip) restore as the
 * same `dynamic-tool` parts the live demux emits, and the resume row that
 * answered them settles them the way the live transport does — so a reload
 * keeps the receipts. A prompt still open at the end of the newest assistant
 * message IS the pending interrupt: `addToolApprovalResponse` + the auto-send
 * resume (and the client-action auto-settle) work identically after a reload.
 */
import {
  extractPartTexts,
  getFileInfo,
  getPartKind,
  getTaskState,
  shouldDisplayMessageParts,
} from '../core/protocol';
import {
  ACTIVITY_LOG_EXT,
  CLIENT_ACTION_EXT,
  HITL_DECISION_EXT,
  HITL_EXT,
  INTERMEDIATE_OUTPUT_EXT,
} from '../core/extensions';
import type { AgentResponseData } from '../core/wire';
import { clientActionPartId, encodeApproval, type Decision } from './approval-codec';
import { textArrival } from './ai-types';
import type { HitlTypedDecision, NannosMessageMetadata, NannosUIMessage } from './ai-types';
import { parseInterrupt, readAuthRequired } from './demux';
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
type ToolPart = Extract<Part, { type: 'dynamic-tool' }>;

type PartMeta = { nannos?: { answeredInChat?: unknown; typedDecision?: HitlTypedDecision } };

/**
 * Whether a prompt was answered by TYPING rather than a click, with no reading
 * of the words on record (yet): its turn moved on without a decision. It is
 * settled so it can never render live buttons, and marked so the thread says
 * what happened instead of claiming a rejection nobody made. A typed decision
 * that later arrives (`typedDecisionOf`) supersedes it.
 */
export function isAnsweredInChat(part: { callProviderMetadata?: unknown }): boolean {
  const meta = part.callProviderMetadata as PartMeta | undefined;
  return meta?.nannos?.answeredInChat === true && !meta.nannos.typedDecision;
}

/** How the server read the words that answered this prompt, when it said. */
export function typedDecisionOf(part: { callProviderMetadata?: unknown }): HitlTypedDecision | undefined {
  return (part.callProviderMetadata as PartMeta | undefined)?.nannos?.typedDecision;
}

/**
 * The receipt a typed answer earns. Only `type: approve` ran the call; among
 * the rest, a refusal is a rejection and a change request reads as one, while a
 * question or an unrelated reply is neither — it stays "answered in chat".
 */
export function typedDecisionOutcome(
  decision: HitlTypedDecision,
): 'approved' | 'rejected' | 'changes' | 'answeredInChat' {
  if (decision.type === 'approve') return 'approved';
  if (decision.intent === 'reject') return 'rejected';
  if (decision.intent === 'change') return 'changes';
  return 'answeredInChat';
}

/** Every typed decision a thread carries, keyed by the prompt (part) id. */
export function typedDecisionsById(messages: NannosUIMessage[]): Map<string, HitlTypedDecision> {
  const byId = new Map<string, HitlTypedDecision>();
  for (const message of messages) {
    for (const part of message.parts) {
      if (part.type !== 'data-hitl-decision') continue;
      for (const decision of part.data.decisions) if (decision.id) byId.set(decision.id, decision);
    }
  }
  return byId;
}

/** Settle an open prompt from its recorded decision — the live transport's
 *  synthetic outputs (a2a-transport `sendMessages`), so the receipt matches. */
function settleFromDecision(part: ToolPart, decision: Decision): void {
  const id = part.toolCallId;
  if (decision.type === 'approve') {
    Object.assign(part, {
      state: 'output-available',
      output: {
        approved: true,
        ...(decision.bypass && { bypass: true }),
        ...(decision.client_action_result && { result: decision.client_action_result }),
      },
      approval: { id, approved: true },
    });
    return;
  }
  // reject / edit: the user's words ride the approval reason, as they do live.
  const reason = decision.message !== undefined ? encodeApproval(decision).reason : undefined;
  Object.assign(part, {
    state: 'output-denied',
    approval: { id, approved: false, ...(reason && { reason }) },
  });
}

/** Settle a prompt from how the server read the words that answered it. */
function settleFromTypedDecision(part: ToolPart, decision: HitlTypedDecision): void {
  const id = part.toolCallId;
  const callProviderMetadata = { nannos: { answeredInChat: true, typedDecision: decision } };
  Object.assign(
    part,
    decision.type === 'approve'
      ? { state: 'output-available', output: { approved: true }, approval: { id, approved: true }, callProviderMetadata }
      : { state: 'output-denied', approval: { id, approved: false }, callProviderMetadata },
  );
}

/**
 * The prompt, settled as answered by typing. Also what the panel does the
 * moment a reply is typed at an open card, so its buttons go inactive at once —
 * the server's reading of the words (`hitl-decision`) refines it later.
 */
export function answeredInChatPart(part: ToolPart): ToolPart {
  return {
    ...part,
    state: 'output-denied',
    approval: { id: part.toolCallId, approved: false },
    callProviderMetadata: { nannos: { answeredInChat: true } },
  } as ToolPart;
}

function settleAnsweredInChat(part: ToolPart): void {
  Object.assign(part, answeredInChatPart(part));
}

/** The decisions a HITL resume row carried (`dataParts: [{decisions}]`). */
function resumeDecisions(row: RestMessageRow): Decision[] {
  const dataParts = parsePayload(row)?.dataParts;
  if (!Array.isArray(dataParts)) return [];
  return dataParts.flatMap((d) => {
    const decisions = (d as { decisions?: unknown } | null)?.decisions;
    return Array.isArray(decisions) ? (decisions as Decision[]) : [];
  });
}

/** The awaited round trip's `{request: {id, directive}}`, or null. */
function clientActionRequest(
  statusMessage: Record<string, unknown> | undefined,
): { id: string; directive: unknown } | null {
  const parts = (statusMessage?.parts ?? []) as Array<Record<string, unknown>>;
  for (const part of parts) {
    if (getPartKind(part) !== 'data') continue;
    const request = (part.data as { request?: { id?: string; directive?: unknown } } | undefined)
      ?.request;
    if (request?.id && request.directive) return { id: request.id, directive: request.directive };
  }
  return null;
}

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
  const display =
    typeof injectedDisplayText === 'string' && injectedDisplayText
      ? injectedDisplay(row, injectedDisplayText)
      : undefined;
  // 0 = the row carried no readable time; leave it unstamped rather than put a
  // 1970 clock in the thread.
  const sentAt = rowTime(row);
  const metadata = { ...(sentAt > 0 && { sentAt }), ...(display && { display }) };
  return {
    id: rowId(row, `hist-u-${index}`),
    role: 'user',
    parts,
    ...(Object.keys(metadata).length > 0 && { metadata }),
  };
}

export interface RowsToUIMessagesOptions {
  /**
   * The page is an OLDER one, with newer messages already in the thread: a
   * prompt still open at its end cannot be the pending interrupt, so it is
   * settled as answered-in-chat instead of left awaiting a decision.
   */
  olderPage?: boolean;
}

/**
 * True when a restored thread stops at a prompt still waiting for its answer:
 * the turn is paused there, so nothing is in flight to reconnect to. The
 * server's snapshot would only replay that prompt — as a SECOND assistant
 * message, i.e. a duplicate live card (and a client action delivered twice).
 */
export function endsWithOpenPrompt(messages: NannosUIMessage[]): boolean {
  const last = messages[messages.length - 1];
  return (
    last?.role === 'assistant' &&
    last.parts.some((part) => part.type === 'dynamic-tool' && part.state === 'approval-requested')
  );
}

/**
 * Map one page of rows (any order; sorted internally by time) into UI
 * messages. Emits complete assistant turns — the caller prepends/replaces via
 * `chat.setMessages` and dedupes on message ids (`persistedMessageId` is set on
 * every assistant message so live-finalized turns reconcile with refetches).
 */
export function rowsToUIMessages(
  rows: RestMessageRow[],
  options: RowsToUIMessagesOptions = {},
): NannosUIMessage[] {
  const sorted = [...rows].sort((a, b) => rowTime(a) - rowTime(b));
  const messages: NannosUIMessage[] = [];

  let assistantParts: Part[] = [];
  let assistantId: string | null = null;
  let assistantHitl: NannosMessageMetadata['hitl'];
  let seq = 0;
  // Every approval part, by its part id (a prompt persisted twice restores
  // once), and the ones still awaiting an answer, in prompt order.
  const toolParts = new Map<string, ToolPart>();
  let openParts: ToolPart[] = [];

  // The turn moved on past an open prompt without a decision on record.
  const abandonOpenParts = () => {
    for (const part of openParts) settleAnsweredInChat(part);
    openParts = [];
  };

  const pushApprovalPart = (part: ToolPart) => {
    if (toolParts.has(part.toolCallId)) return;
    toolParts.set(part.toolCallId, part);
    openParts.push(part);
    assistantParts.push(part);
  };

  const flushAssistant = () => {
    if (assistantParts.length === 0) {
      assistantId = null;
      assistantHitl = undefined;
      return;
    }
    const id = assistantId ?? `hist-a-${messages.length}`;
    messages.push({
      id,
      role: 'assistant',
      parts: assistantParts,
      metadata: { persistedMessageId: id, ...(assistantHitl && { hitl: assistantHitl }) },
    });
    assistantParts = [];
    assistantId = null;
    assistantHitl = undefined;
  };

  for (const [index, row] of sorted.entries()) {
    const role = row.role ?? (row.user_id ? 'user' : 'agent');
    if (role === 'user') {
      // A HITL RESUME carries its decisions (or a client-action result) on
      // `dataParts`: settle the prompts it answered, by the same part id the
      // live answer came from — a client-action REQUEST's derived id first,
      // since a risk gate on the same call was settled before it was asked.
      for (const decision of resumeDecisions(row)) {
        const part = decision.id
          ? (openParts.find((p) => p.toolCallId === clientActionPartId(decision.id!)) ??
            openParts.find((p) => p.toolCallId === decision.id))
          : openParts[0];
        if (!part) continue;
        settleFromDecision(part, decision);
        openParts = openParts.filter((p) => p !== part);
      }
      const message = userMessage(row, index);
      // The resume itself is persisted as a user row with an EMPTY message. It
      // is nothing the user said and has nothing to show, so it renders no
      // bubble AND does not break the turn — the agent parts on either side of
      // the approval belong to one assistant message, exactly as the live path
      // streams them.
      if (message.parts.length === 0) continue;
      // The user typed instead of answering: whatever was still open stays
      // behind, settled, in the turn it belonged to.
      abandonOpenParts();
      flushAssistant();
      messages.push(message);
      continue;
    }

    const payload = parsePayload(row);
    const facts = payloadFacts(payload);
    const time = rowTime(row) || Date.now();
    seq += 1;

    // Any later status the agent sent means its turn went on: a prompt still
    // open was never answered by a decision.
    if (row.kind === 'status-update' && getTaskState(row.state) !== 'input-required') {
      abandonOpenParts();
    }

    // How the server read a reply the user TYPED at an approval card: the card
    // (already settled as answered-in-chat by the typed row) takes the real
    // outcome. Renders nothing itself.
    if (facts.statusExtensions.includes(HITL_DECISION_EXT)) {
      const parts = (facts.statusMessage?.parts ?? []) as Array<Record<string, unknown>>;
      for (const part of parts) {
        if (getPartKind(part) !== 'data') continue;
        const decisions = (part.data as { decisions?: unknown } | undefined)?.decisions;
        if (!Array.isArray(decisions)) continue;
        for (const decision of decisions as HitlTypedDecision[]) {
          const target = decision.id ? toolParts.get(decision.id) : undefined;
          if (!target) continue;
          settleFromTypedDecision(target, decision);
          openParts = openParts.filter((p) => p !== target);
        }
      }
      continue;
    }

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
    // card instead. It restores as the SAME `approval-requested` parts the live
    // demux emits (#13a / #8), ids included; the resume row settles them into
    // their receipts. Plain `input-required` rows (no extension) are a real
    // question to the user and still fall through to the text branch.
    if (row.kind === 'status-update' && state === 'input-required') {
      if (facts.statusExtensions.includes(HITL_EXT)) {
        const interrupt = parseInterrupt(payload as unknown as AgentResponseData);
        const firstAction = interrupt.actionRequests[0];
        assistantHitl = {
          reason:
            (firstAction?.args?.description as string) ||
            (firstAction?.args?.reason as string) ||
            interrupt.reason,
          reviewConfigs: interrupt.reviewConfigs,
        };
        for (const [i, action] of interrupt.actionRequests.entries()) {
          const callId = (action.args?._call_id as string) || `hist-call-${seq}-${i}`;
          pushApprovalPart({
            type: 'dynamic-tool',
            toolName: action.name,
            toolCallId: callId,
            state: 'approval-requested',
            input: action.args ?? {},
            approval: { id: callId },
          });
        }
        continue;
      }
      if (facts.statusExtensions.includes(CLIENT_ACTION_EXT)) {
        // Marked `_clientActionRequest`: useNannosChat answers it itself — and
        // re-executes it on reload when it is still open.
        const request = clientActionRequest(facts.statusMessage);
        if (request) {
          const partId = clientActionPartId(request.id);
          pushApprovalPart({
            type: 'dynamic-tool',
            toolName: 'client_action',
            toolCallId: partId,
            state: 'approval-requested',
            input: { directive: request.directive, _clientActionRequest: true },
            approval: { id: partId },
          });
        }
        continue;
      }
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
  // Whatever is still open now ends the newest assistant message: that is the
  // pending interrupt — unless newer messages already follow this page.
  if (options.olderPage) abandonOpenParts();
  flushAssistant();
  return messages;
}
