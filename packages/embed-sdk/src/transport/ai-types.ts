/**
 * The Nannos chat message model on top of the AI SDK's `UIMessage`, plus the
 * single import barrel for everything we take from `ai`. Both `ai` and
 * `@ai-sdk/react` are exact-pinned, BUNDLED dependencies of this SDK — hosts
 * never install them — so every `ai` symbol used anywhere in the SDK must be
 * imported through this file. A version bump is then one edit + one review of
 * this contract, not a repo-wide grep.
 */
import type { ProviderMetadata, UIMessage, UIMessageChunk, InferUIMessageChunk } from 'ai';

/** A work-plan step (mirrors the backend's todo snapshot payload). */
export interface TodoItem {
  name: string;
  state: 'submitted' | 'working' | 'completed' | 'failed';
  /** Sub-agent that owns the step; absent/'' = the orchestrator's own plan. */
  source?: string;
  target?: string;
}

/** One HITL review config row: which decisions the card may offer for a tool. */
export interface ReviewConfig {
  action_name: string;
  allowed_decisions: string[];
}

/**
 * Typed data parts carried inside a `NannosUIMessage`. Parts with a stable id
 * are RECONCILED in place by the AI SDK (re-emitting the same id updates the
 * part); `transient` parts are delivered to `onData` and never persisted into
 * the message — they feed conversation-level UI state (task panel, banners).
 *
 * `wire`/`wireId` are dev-mode provenance: the wire-log label of the raw event
 * that produced the part (`labelAgentEvent`, e.g. 'status-update ·
 * input-required · hitl') and that event's wire-log entry id, which is how the
 * thread's dev badge pulls up the full raw payload. Stamped by the demux on
 * live turns only — a part restored from history carries neither, and nothing
 * but the dev badge reads them.
 */
export type NannosDataParts = {
  /** The live work plan; single part id 'workplan', updated in place. Persisted. */
  workplan: { todos: TodoItem[]; wire?: string; wireId?: string };
  /** One sub-agent thought; id-reconciled while streaming (`complete` flips on close). Persisted. */
  'agent-thought': { agent: string; text: string; complete: boolean; startedAt: number; wire?: string; wireId?: string };
  /** One activity-log line (tool call, delegation, progress). Append-only ids. Persisted.
   *  `kind: 'note'` marks a MID-TURN NOTE: the agent's own words for the user
   *  (`notify_user`), not a machine label — the thread renders it as speech. */
  activity: { text: string; source?: string; kind?: 'note'; ts: number; wire?: string; wireId?: string };
  /** A tool needs the user's secondary authorization. The panel renders its OWN
   *  localized prompt from `authUrl`/`tool`; `message` is the gateway's text,
   *  addressed to the agent, kept only as a last-resort fallback and for dev
   *  mode. Persisted. */
  'auth-required': { authUrl?: string; tool?: string; message?: string; wire?: string; wireId?: string };
  /** Task status for the task panel. Transient. */
  task: { taskId?: string; state: string; progress?: number; title?: string };
  /** Proactive feedback prompt. Transient. */
  'feedback-request': { subAgents: string[] };
};

export interface NannosMessageMetadata {
  /** DB id the backend injects mid-turn; used as the history dedupe key. */
  persistedMessageId?: string;
  /** Host-injected prompt rendered as a muted context chip instead of a user bubble. */
  display?: { kind: 'context'; label: string };
  /** Send-side file info (wire shape, incl. s3Url); `file` parts carry the render shape. */
  attachments?: Array<{ uri: string; mimeType: string; name: string; s3Url?: string }>;
  /** HITL envelope for the current interrupt: reason text + per-tool decision gating. */
  hitl?: { reason: string; reviewConfigs: ReviewConfig[] };
}

/**
 * Arrival stamp for a TEXT part. Text chunks carry no data of ours, and
 * `providerMetadata` is the one per-part slot the AI SDK forwards from
 * `text-start` onto the built part — so that is where the answer's arrival
 * time rides, the way an `activity` part carries `data.ts` — and, when given,
 * the wire label of the event that opened the part (see `NannosDataParts`).
 * Dev mode is the only reader; nothing on the wire depends on it.
 */
export function textArrival(ts: number, wire?: string, wireId?: string): ProviderMetadata {
  return { nannos: { ts, ...(wire && { wire }), ...(wireId && { wireId }) } };
}

/** The arrival stamp back off a text part; undefined when it carries none. */
export function textArrivalTs(part: { providerMetadata?: ProviderMetadata }): number | undefined {
  const ts = part.providerMetadata?.nannos?.ts;
  return typeof ts === 'number' ? ts : undefined;
}

/** The wire label back off a text part; undefined when it carries none. */
export function textWire(part: { providerMetadata?: ProviderMetadata }): string | undefined {
  const wire = part.providerMetadata?.nannos?.wire;
  return typeof wire === 'string' ? wire : undefined;
}

/** The wire-log entry id back off a text part; undefined when it carries none. */
export function textWireId(part: { providerMetadata?: ProviderMetadata }): string | undefined {
  const wireId = part.providerMetadata?.nannos?.wireId;
  return typeof wireId === 'string' ? wireId : undefined;
}

/**
 * Stamp for a FILE part the agent produced mid-turn. The AI SDK's `file` chunk
 * has no filename slot (only url + mediaType), so the name rides here; the
 * history path sets `filename` on the part directly — `fileName` reads both.
 */
export function fileArrival(
  name: string | undefined,
  ts: number,
  wire?: string,
  wireId?: string,
): ProviderMetadata {
  return { nannos: { ts, ...(name && { filename: name }), ...(wire && { wire }), ...(wireId && { wireId }) } };
}

/** A file part's display name: `filename` (history), the live stamp, else its URL. */
export function fileName(part: { url: string; filename?: string; providerMetadata?: ProviderMetadata }): string {
  if (part.filename) return part.filename;
  const stamped = part.providerMetadata?.nannos?.filename;
  return typeof stamped === 'string' && stamped ? stamped : part.url;
}

/**
 * All HITL actions surface as `dynamic-tool` parts (tool names are
 * server-defined), so the tools generic stays open.
 */
export type NannosUIMessage = UIMessage<NannosMessageMetadata, NannosDataParts>;

export type NannosUIMessageChunk = InferUIMessageChunk<NannosUIMessage>;

/** Untyped chunk alias for the ChatTransport interface boundary. */
export type AnyUIMessageChunk = UIMessageChunk;

export type {
  ProviderMetadata,
  UIMessage,
  UIMessageChunk,
  ChatTransport,
  ChatRequestOptions,
  ChatStatus,
  DynamicToolUIPart,
} from 'ai';
export {
  isToolUIPart,
  lastAssistantMessageIsCompleteWithApprovalResponses,
  lastAssistantMessageIsCompleteWithToolCalls,
} from 'ai';
