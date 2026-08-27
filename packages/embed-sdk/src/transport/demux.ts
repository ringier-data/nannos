/**
 * The A2A → UIMessageChunk demultiplexer: a pure function from one
 * `agent_response` event (plus per-turn state) to the chunk sequence the AI
 * SDK consumes. Ported branch-by-branch from the retired ChatContext handler
 * (ChatContext.tsx:452-1026 at tag embed-sdk-v1) — comments cite the original
 * branch numbers so behavior stays auditable against the old implementation.
 *
 * No I/O here: routing (which conversation), stream lifecycle, and socket
 * emits live in the transport; client-action directives are executed by the
 * core registry listener and deliberately produce NO chunks.
 */
import type { AgentResponseData } from '../core/wire';
import { clientActionPartId } from './approval-codec';
import {
  extractPartTexts,
  getPartKind,
  getTaskState,
  shouldDisplayMessageParts,
} from '../core/protocol';
import {
  ACTIVITY_LOG_EXT,
  CLIENT_ACTION_EXT,
  FEEDBACK_REQUEST_EXT,
  HITL_EXT,
  INTERMEDIATE_OUTPUT_EXT,
  WORK_PLAN_EXT,
} from '../core/extensions';
import { generateUUID } from '../core/protocol';
import { labelAgentEvent } from './wire-log';
import { textArrival } from './ai-types';
import type {
  NannosMessageMetadata,
  NannosUIMessageChunk,
  ReviewConfig,
  TodoItem,
} from './ai-types';

export type DemuxDone = 'terminal' | 'input-required' | 'error';

export interface DemuxResult {
  chunks: NannosUIMessageChunk[];
  /** Set when this event ends the turn — the session closes its stream. */
  done?: DemuxDone;
  /** The backend acked a steering send; nothing rendered, transport bookkeeping only. */
  steering?: boolean;
}

interface OpenThought {
  partId: string;
  agent: string;
  text: string;
  startedAt: number;
  /** Wire label + log id of the event that OPENED the thought — appends don't change them. */
  wire: string;
  wireId?: string;
}

/** A durable (non-text) part this turn emitted, in emission order — the replay
 *  log for `reset-step` supersedes (workplan/thought entries update in place). */
type DurablePart =
  | { type: 'data-workplan'; id: string; data: { todos: TodoItem[]; wire?: string; wireId?: string } }
  | { type: 'data-agent-thought'; id: string; data: { agent: string; text: string; complete: boolean; startedAt: number; wire?: string; wireId?: string } }
  | { type: 'data-activity'; id: string; data: { text: string; source?: string; ts: number; wire?: string; wireId?: string } }
  | { type: 'data-auth-required'; id: string; data: { authUrl?: string; tool?: string; message?: string; wire?: string; wireId?: string } };

/** Per-turn mutable state owned by the TurnSession; demux() mutates it. */
export interface DemuxState {
  /**
   * Unique per-turn prefix on generated part ids. Part ids reconcile within a
   * message, and a HITL resume CONTINUES the interrupted assistant message —
   * unprefixed ids from the resumed turn would silently overwrite the earlier
   * turn's parts. ('workplan' stays unprefixed on purpose: updating the same
   * plan part across resumed turns of one message is the wanted behavior.)
   */
  idPrefix: string;
  /** Open streamed-text part id, or null when no text part is open. */
  textId: string | null;
  /** Mirror of the streamed text — only for the fallback-supersede comparison. */
  textBuffer: string;
  /** Cumulative reply length in CODE POINTS. Assign ONLY from server numbers. */
  appliedOffset: number;
  openThought: OpenThought | null;
  thoughtSeq: number;
  activitySeq: number;
  textSeq: number;
  todos: TodoItem[];
  /** Ordered log of this turn's durable parts, replayed after a reset-step. */
  durable: DurablePart[];
  /** Accumulated message metadata; emitted whole so merge-vs-replace semantics don't matter. */
  metadata: NannosMessageMetadata;
}

export function createDemuxState(idPrefix: string): DemuxState {
  return {
    idPrefix,
    textId: null,
    textBuffer: '',
    appliedOffset: 0,
    openThought: null,
    thoughtSeq: 0,
    activitySeq: 0,
    textSeq: 0,
    todos: [],
    durable: [],
    metadata: {},
  };
}

const TERMINAL_STATES = new Set([
  'completed',
  'failed',
  'canceled',
  'input-required',
  // `auth-required` IS terminal: the orchestrator's A2A stream ends on the auth
  // interrupt and only a fresh send resumes the turn (executor.py, the
  // auth_required branch). Leaving it out kept the turn open forever and
  // dropped the status text on the floor.
  'auth-required',
]);

/** First http(s) URL in a text blob — the fallback when metadata has no auth_url. */
const URL_IN_TEXT = /https?:\/\/[^\s<>"')]+/;

/**
 * The authorize target for an `auth-required` status. The orchestrator puts it
 * in the status metadata (`auth_url`, plus the `tool` that asked); older/other
 * shapes only spell it out in the message text, so we scan that too.
 *
 * Shared with the history mapper, so a reloaded conversation restores the same
 * part (and therefore the same localized card) as the live turn.
 */
export function readAuthRequired(
  statusText: string,
  meta: Record<string, unknown> | undefined,
): { authUrl?: string; tool?: string; message?: string } {
  const text = statusText.trim();
  const fromMeta = meta?.auth_url ?? meta?.authUrl ?? meta?.authorizeUrl;
  const authUrl =
    typeof fromMeta === 'string' && fromMeta ? fromMeta : (text.match(URL_IN_TEXT)?.[0] ?? undefined);
  const tool = typeof meta?.tool === 'string' && meta.tool ? meta.tool : undefined;
  return { ...(authUrl && { authUrl }), ...(tool && { tool }), ...(text && { message: text }) };
}

function statusExtensions(data: AgentResponseData): string[] {
  return (data.status?.message?.extensions ?? []) as string[];
}

function findDataPart(parts: Array<{ kind?: string; data?: unknown }> | undefined) {
  return parts?.find((p) => p.kind === 'data' || (p as { data?: unknown }).data);
}

function metadataChunk(state: DemuxState): NannosUIMessageChunk {
  return { type: 'message-metadata', messageMetadata: { ...state.metadata } };
}

/** Record/refresh a durable part in the replay log (workplan + open thoughts update in place). */
function logDurable(state: DemuxState, part: DurablePart) {
  const existing = state.durable.find((p) => p.type === part.type && p.id === part.id);
  if (existing) {
    existing.data = part.data as never;
  } else {
    state.durable.push(part);
  }
}

/** Close the open sub-agent thought, re-emitting it with complete:true. */
function closeThought(state: DemuxState, out: NannosUIMessageChunk[]) {
  const open = state.openThought;
  if (!open) return;
  const data = {
    agent: open.agent,
    text: open.text,
    complete: true,
    startedAt: open.startedAt,
    wire: open.wire,
    wireId: open.wireId,
  };
  logDurable(state, { type: 'data-agent-thought', id: open.partId, data });
  out.push({ type: 'data-agent-thought', id: open.partId, data });
  state.openThought = null;
}

/** Close the open streamed-text part. */
function closeText(state: DemuxState, out: NannosUIMessageChunk[]) {
  if (!state.textId) return;
  out.push({ type: 'text-end', id: state.textId });
  state.textId = null;
}

/**
 * Emit `fullText` as the authoritative text, reconciling with any open stream:
 * - stream empty → plain new text part;
 * - fullText extends the stream → emit just the remainder;
 * - fullText diverges → `reset-step` retracts everything this step streamed
 *   (the stub text AND the durable parts, which are replayed from the log),
 *   then the authoritative text is emitted fresh. This is the honest encoding
 *   of "the terminal message supersedes the stream" (original #10/#12/#13
 *   fallback semantics) — text parts carry no ids in the final message, so a
 *   marker-based convention can't work.
 */
function emitAuthoritativeText(
  state: DemuxState,
  out: NannosUIMessageChunk[],
  fullText: string,
  wire: string,
  wireId?: string,
) {
  const streamed = state.textBuffer;
  if (state.textId && streamed && fullText.startsWith(streamed)) {
    const remainder = fullText.slice(streamed.length);
    if (remainder) out.push({ type: 'text-delta', id: state.textId, delta: remainder });
    state.textBuffer = fullText;
    closeText(state, out);
    return;
  }
  if (state.textId && streamed.trim()) {
    // Divergent: retract the step and replay the durable parts.
    state.textId = null;
    out.push({ type: 'reset-step' });
    for (const part of state.durable) out.push({ ...part });
  } else {
    closeText(state, out);
  }
  state.textSeq += 1;
  const id = `${state.idPrefix}txt-${state.textSeq}`;
  out.push({ type: 'text-start', id, providerMetadata: textArrival(Date.now(), wire, wireId) });
  out.push({ type: 'text-delta', id, delta: fullText });
  out.push({ type: 'text-end', id });
  state.textBuffer = fullText;
}

/** HITL interrupt payload parsed from the input-required status message. */
interface ParsedInterrupt {
  reason: string;
  actionRequests: Array<{ name: string; args: Record<string, unknown>; description?: string }>;
  reviewConfigs: ReviewConfig[];
}

function parseInterrupt(data: AgentResponseData): ParsedInterrupt {
  const parsed: ParsedInterrupt = { reason: '', actionRequests: [], reviewConfigs: [] };
  for (const part of data.status?.message?.parts ?? []) {
    const kind = getPartKind(part);
    if (kind === 'data') {
      const d = (part as { data?: Record<string, unknown> }).data;
      if (Array.isArray(d?.action_requests)) {
        parsed.actionRequests = d.action_requests as ParsedInterrupt['actionRequests'];
      }
      if (Array.isArray(d?.review_configs)) {
        parsed.reviewConfigs = d.review_configs as ReviewConfig[];
      }
    } else if (kind === 'text') {
      parsed.reason = (part as { text?: string }).text || '';
    }
  }
  return parsed;
}

/** Which kind of answer an `input-required` prompt waits for. */
export type ApprovalPromptKind = 'hitl' | 'client-action';

export interface ApprovalPrompt {
  /** `hitl` — a human decision on a tool call; `client-action` — the awaited
   *  round trip the BROWSER answers with its execution result. */
  kind: ApprovalPromptKind;
  ids: string[];
}

/**
 * The approval prompt an `input-required` event carries — HITL action requests
 * (#13a) or a client-action round trip (#8) — as its kind plus the call ids it
 * asks about. Returns null when the event is not an approval prompt, or when
 * any request carries no stable `_call_id`: with nothing to match on it must
 * always render.
 *
 * Used by the TurnSession to recognise a prompt it has ALREADY answered. The
 * id is `ptc_guard._call_key` — a hash of tool + args, deterministic within a
 * turn, and the same key the middleware itself dedupes on server-side.
 *
 * The KIND is part of that identity because the two prompt shapes SHARE one
 * id: a risk-gated `client_action` call is approved under its `_call_id`, and
 * the round-trip request the tool then emits reuses that same id (it IS the
 * injected `tool_call_id`). Matching on the id alone made the request look
 * like a replay of the approval just answered — it was dropped, nothing
 * executed in the page, and the turn parked forever.
 */
export function approvalPrompt(data: AgentResponseData): ApprovalPrompt | null {
  if (data.kind !== 'status-update') return null;
  if (getTaskState(data.status?.state) !== 'input-required') return null;
  const exts = statusExtensions(data);
  if (exts.includes(CLIENT_ACTION_EXT)) {
    const request = (
      findDataPart(data.status?.message?.parts) as
        | { data?: { request?: { id?: string; directive?: unknown } } }
        | undefined
    )?.data?.request;
    return request?.id && request.directive ? { kind: 'client-action', ids: [request.id] } : null;
  }
  if (!exts.includes(HITL_EXT)) return null;
  const ids = parseInterrupt(data).actionRequests.map((a) => a.args?._call_id);
  if (ids.length === 0 || ids.some((id) => typeof id !== 'string' || !id)) return null;
  return { kind: 'hitl', ids: ids as string[] };
}

/**
 * Process one `agent_response` event. Mutates `state`; returns the chunks to
 * enqueue plus lifecycle signals. Events that resolve to no session, and
 * client-action directives, are the caller's concern.
 */
export function demux(state: DemuxState, data: AgentResponseData, wireId?: string): DemuxResult {
  const out: NannosUIMessageChunk[] = [];

  // #1 Steering ack — the follow-up was queued for the running agent; nothing renders.
  if ((data as { steering?: boolean }).steering) {
    return { chunks: [], steering: true };
  }

  // Dev-mode provenance: every part this event produces is stamped with the
  // SAME label the wire log gives the raw event, so the thread's dev badge
  // and the inspector's wire tab speak one language.
  const wire = labelAgentEvent(data);

  // #3 Work plan (todo snapshot) — merge by source, orchestrator ('') first. (:489-518)
  const exts = statusExtensions(data);
  if (exts.includes(WORK_PLAN_EXT) && Array.isArray(data.status?.message?.parts)) {
    const dataPart = findDataPart(data.status.message.parts);
    const incoming = ((dataPart as { data?: { todos?: TodoItem[] } })?.data?.todos ?? []) as TodoItem[];
    if (incoming.length > 0) {
      const incomingSources = new Set(incoming.map((t) => t.source || ''));
      const retained = state.todos.filter((t) => !incomingSources.has(t.source || ''));
      const merged = [...retained, ...incoming].sort((a, b) => {
        const aSource = a.source || '';
        const bSource = b.source || '';
        if (!aSource && bSource) return -1;
        if (aSource && !bSource) return 1;
        return aSource.localeCompare(bSource);
      });
      state.todos = merged;
      const planData = { todos: merged, wire, wireId };
      logDurable(state, { type: 'data-workplan', id: 'workplan', data: planData });
      out.push({ type: 'data-workplan', id: 'workplan', data: planData });
      return { chunks: out };
    }
  }

  // #4 Persisted DB id — captured mid-turn, becomes the history dedupe key. (:521-523)
  if (data.persistedMessageId) {
    state.metadata.persistedMessageId = data.persistedMessageId;
    out.push(metadataChunk(state));
  }

  // #5/#6 Streaming artifact chunks. (:526-582)
  if (data.kind === 'artifact-update' && Array.isArray(data.artifact?.parts)) {
    const text = extractPartTexts(data.artifact.parts).join('');
    if (!text) return { chunks: out };

    const artifactExts = (data.artifact.extensions ?? []) as string[];
    const agent =
      ((data.artifact.metadata as Record<string, unknown> | undefined)?.agent_name as string) ||
      'sub-agent';

    if (artifactExts.includes(INTERMEDIATE_OUTPUT_EXT)) {
      // #5 Sub-agent thought accumulation: append to the open thought of the
      // same agent, else close it and open a new one. (:537-552)
      if (state.openThought && state.openThought.agent === agent) {
        state.openThought.text += text;
      } else {
        closeThought(state, out);
        state.thoughtSeq += 1;
        state.openThought = {
          partId: `${state.idPrefix}thought-${state.thoughtSeq}`,
          agent,
          text,
          startedAt: Date.now(),
          wire,
          wireId,
        };
      }
      const t = state.openThought;
      const thoughtData = {
        agent: t.agent,
        text: t.text,
        complete: false,
        startedAt: t.startedAt,
        wire: t.wire,
        wireId: t.wireId,
      };
      logDurable(state, { type: 'data-agent-thought', id: t.partId, data: thoughtData });
      out.push({ type: 'data-agent-thought', id: t.partId, data: thoughtData });
      return { chunks: out };
    }

    // #6 Orchestrator reply chunk: thinking is over; dedupe by CODE-POINT offset
    // (never .length — Python len vs UTF-16, the emoji bug). (:553-577)
    closeThought(state, out);
    const chunkOffset = data.turnOffset;
    if (typeof chunkOffset === 'number' && chunkOffset <= state.appliedOffset) {
      return { chunks: out };
    }
    if (!state.textId) {
      state.textSeq += 1;
      state.textId = `${state.idPrefix}txt-${state.textSeq}`;
      // Stamped at the FIRST token: dev mode times the answer's arrival the
      // way it times activity lines.
      out.push({
        type: 'text-start',
        id: state.textId,
        providerMetadata: textArrival(Date.now(), wire, wireId),
      });
    }
    out.push({ type: 'text-delta', id: state.textId, delta: text });
    state.textBuffer += text;
    if (typeof chunkOffset === 'number') state.appliedOffset = chunkOffset;
    return { chunks: out };
  }

  if (data.kind === 'status-update') {
    // #7 Feedback request → transient banner data. (:585-596)
    if (exts.includes(FEEDBACK_REQUEST_EXT)) {
      const dataPart = findDataPart(data.status?.message?.parts);
      const subAgents = ((dataPart as { data?: { sub_agents?: string[] } })?.data?.sub_agents ??
        []) as string[];
      out.push({ type: 'data-feedback-request', data: { subAgents }, transient: true });
      return { chunks: out };
    }

    // #8 Client-action events. Two shapes ride this extension:
    //  - `{directive}` (fire-and-forget): NOT chat content — the core registry
    //    listener executes it; the chat stream ignores it entirely. (:601-633)
    //  - `{request: {id, directive}}` with input-required: a ROUND TRIP — the
    //    paused `client_action` tool awaits the browser's result. Surfaced as a
    //    dynamic-tool part in the HITL approval shape, but marked
    //    `_clientActionRequest`: useNannosChat AUTO-settles it (executes the
    //    directive, answers with the result) instead of rendering a card. The
    //    resume then rides the normal approval-response machinery.
    if (exts.includes(CLIENT_ACTION_EXT)) {
      const dataPart = findDataPart(data.status?.message?.parts);
      const request = (dataPart as { data?: { request?: { id?: string; directive?: unknown } } })
        ?.data?.request;
      if (
        request?.id &&
        request.directive &&
        getTaskState(data.status?.state) === 'input-required'
      ) {
        closeThought(state, out);
        closeText(state, out);
        // Its OWN part id: a risk-gated `client_action` was already approved
        // under this very call id, and that part is settled — see
        // `clientActionPartId`. Stripped again on the way back out.
        const partId = clientActionPartId(request.id);
        out.push({
          type: 'tool-input-start',
          toolCallId: partId,
          toolName: 'client_action',
          dynamic: true,
        });
        out.push({
          type: 'tool-input-available',
          toolCallId: partId,
          toolName: 'client_action',
          input: { directive: request.directive, _clientActionRequest: true },
          dynamic: true,
        });
        out.push({ type: 'tool-approval-request', approvalId: partId, toolCallId: partId });
        return { chunks: out, done: 'input-required' };
      }
      return { chunks: out };
    }

    // #9 Activity log line, with source attribution; closes open thinking. (:637-669)
    if (exts.includes(ACTIVITY_LOG_EXT)) {
      const msg = data.status?.message;
      const text = Array.isArray(msg?.parts) ? extractPartTexts(msg.parts).join('') : '';
      if (text.trim()) {
        closeThought(state, out);
        const meta = msg?.metadata as Record<string, unknown> | undefined;
        const source = meta?.source;
        // A mid-turn note (notify_user) rides the same extension with kind='note'.
        // Unknown kinds are dropped: an older/newer agent must not make the line
        // render as something this build has no styling for.
        const isNote = meta?.kind === 'note';
        state.activitySeq += 1;
        const activity = {
          text,
          ...(typeof source === 'string' && { source }),
          ...(isNote && { kind: 'note' as const }),
          ts: Date.now(),
          wire,
          wireId,
        };
        const actId = `${state.idPrefix}act-${state.activitySeq}`;
        logDurable(state, { type: 'data-activity', id: actId, data: activity });
        out.push({ type: 'data-activity', id: actId, data: activity });
        return { chunks: out };
      }
    }
  }

  // #11 Error → close the turn. (:682-699)
  if (data.error) {
    closeThought(state, out);
    closeText(state, out);
    out.push({ type: 'error', errorText: data.error });
    return { chunks: out, done: 'error' };
  }

  // #10+#12 Full agent message with displayable parts, mid-turn: authoritative
  // text superseding any partial stream. (:673-679, :702-728)
  if (data.role === 'agent' && Array.isArray(data.parts)) {
    if (shouldDisplayMessageParts(data.parts)) {
      const text = extractPartTexts(data.parts).join('\n');
      closeThought(state, out);
      emitAuthoritativeText(state, out, text, wire, wireId);
    }
    return { chunks: out };
  }

  // #13 Task status updates. (:731-955)
  if (data.status) {
    const normalized = getTaskState(data.status.state);
    const isTerminal = TERMINAL_STATES.has(normalized);
    let finalizedFromStream = false;

    if (isTerminal) {
      closeThought(state, out);

      // The backend tags the terminal status `final_answer_source: 'fallback'`
      // when the streamed artifact was only a stub — the terminal message is
      // then authoritative. (:775-792)
      const statusMeta = (data.metadata ??
        (data.status as { metadata?: Record<string, unknown> }).metadata ??
        data.status.message?.metadata) as Record<string, unknown> | undefined;
      const fallbackSupersedes =
        normalized === 'completed' && statusMeta?.final_answer_source === 'fallback';

      if (state.textBuffer.trim() && !fallbackSupersedes) {
        finalizedFromStream = true;
      }

      // #13a-auth Secondary authorization → ONE structured part, and the turn
      // ends. The status text is NOT rendered: it is the MCP gateway's message
      // to the agent ("You must tell the end-user to…"), which no LLM ever gets
      // to rewrite (the auth interrupt fires in middleware, before the model).
      // The panel writes its own localized copy from `authUrl`/`tool` instead.
      if (normalized === 'auth-required') {
        closeText(state, out);
        const auth = readAuthRequired(
          extractPartTexts(data.status.message?.parts).join('\n'),
          statusMeta,
        );
        const authId = `${state.idPrefix}auth`;
        const authData = { ...auth, wire, wireId };
        logDurable(state, { type: 'data-auth-required', id: authId, data: authData });
        out.push({ type: 'data-auth-required', id: authId, data: authData });
        return { chunks: out, done: 'terminal' };
      }

      // #13a HITL interrupt → native tool-approval parts, then the stream ends
      // (the backend's A2A stream closed on the LangGraph interrupt; the turn
      // resumes only via a fresh send). (:805-847)
      if (normalized === 'input-required' && exts.includes(HITL_EXT)) {
        const interrupt = parseInterrupt(data);
        closeText(state, out);
        const firstAction = interrupt.actionRequests[0];
        state.metadata.hitl = {
          reason:
            (firstAction?.args?.description as string) ||
            (firstAction?.args?.reason as string) ||
            interrupt.reason,
          reviewConfigs: interrupt.reviewConfigs,
        };
        out.push(metadataChunk(state));
        for (const action of interrupt.actionRequests) {
          const callId = (action.args?._call_id as string) || generateUUID();
          out.push({
            type: 'tool-input-start',
            toolCallId: callId,
            toolName: action.name,
            dynamic: true,
          });
          out.push({
            type: 'tool-input-available',
            toolCallId: callId,
            toolName: action.name,
            input: action.args ?? {},
            dynamic: true,
          });
          out.push({ type: 'tool-approval-request', approvalId: callId, toolCallId: callId });
        }
        return { chunks: out, done: 'input-required' };
      }
    }

    // #13b/#13c Nested status message text. (:851-895)
    const nested = data.status.message;
    if (
      !finalizedFromStream &&
      nested &&
      Array.isArray(nested.parts) &&
      shouldDisplayMessageParts(nested.parts)
    ) {
      const text = extractPartTexts(nested.parts).join('\n');
      if (normalized === 'working') {
        // Sub-agent progress line ("Initiating call…") → activity timeline.
        state.activitySeq += 1;
        const actId = `${state.idPrefix}act-${state.activitySeq}`;
        const activity = { text, ts: Date.now(), wire, wireId };
        logDurable(state, { type: 'data-activity', id: actId, data: activity });
        out.push({ type: 'data-activity', id: actId, data: activity });
      } else if (isTerminal) {
        emitAuthoritativeText(state, out, text, wire, wireId);
      }
    }

    // #13d Task upsert → transient task panel data. (:897-954)
    const progress =
      typeof data.progress === 'number'
        ? data.progress
        : typeof data.status.progress === 'number'
          ? data.status.progress
          : undefined;
    out.push({
      type: 'data-task',
      id: `task-${data.id ?? 'main'}`,
      data: {
        ...(data.taskId ? { taskId: data.taskId } : data.id ? { taskId: data.id } : {}),
        state: normalized,
        ...(progress !== undefined && { progress }),
        ...(data.title && { title: data.title }),
      },
      transient: true,
    });

    if (isTerminal) {
      closeText(state, out);
      return { chunks: out, done: normalized === 'input-required' ? 'input-required' : 'terminal' };
    }
    return { chunks: out };
  }

  // #14 Artifact fallthrough (rare task-result artifacts outside #5/#6). (:958-1011)
  if (data.artifact || data.kind === 'artifact-update') {
    const art =
      data.artifact ??
      (Array.isArray(data.artifacts)
        ? (data.artifacts as Array<{ parts?: Array<{ text?: string }> }>)[0]
        : null);
    if (art && Array.isArray(art.parts) && shouldDisplayMessageParts(art.parts)) {
      emitAuthoritativeText(state, out, extractPartTexts(art.parts).join('\n'), wire, wireId);
    }
    out.push({
      type: 'data-task',
      id: `task-${data.id ?? 'main'}`,
      data: { state: 'completed', progress: 100 },
      transient: true,
    });
    return { chunks: out };
  }

  // #15 Unknown event: drop (the old JSON.stringify bubble is deliberately gone).
  return { chunks: out };
}
