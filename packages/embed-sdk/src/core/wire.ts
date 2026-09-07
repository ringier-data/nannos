// Wire types for the console-backend socket.io protocol — the payload shapes
// exchanged over `send_message` / `agent_response` / `initialize_client` /
// `client_initialized`. Framework-free; UI-side types (Message, Conversation,
// Task) stay with the UI kit.

export interface TaskHistoryEntry {
  contextId?: string | null;
  kind?: string;
  messageId?: string | null;
  parts?: Array<{ kind?: string; text?: string }>;
  role?: string;
  taskId?: string | null;
}

export interface TaskStatusDetails {
  state?: string;
  label?: string;
  message?: {
    parts?: Array<{ text?: string; kind?: string; data?: unknown; media_type?: string }>;
    contextId?: string;
    messageId?: string;
    kind?: string;
    role?: string;
    taskId?: string;
    extensions?: string[];
    metadata?: Record<string, unknown>;
  };
  progress?: number;
}

export interface AgentInfo {
  name?: string;
  title?: string;
  displayName?: string;
  url?: string;
  version?: string;
  description?: string;
  protocolVersion?: string;
  preferredTransport?: string;
  capabilities?: {
    pushNotifications?: boolean;
    streaming?: boolean;
  };
  skills?: Array<{
    id?: string;
    name?: string;
    description?: string;
    examples?: string[];
  }>;
}

export interface Settings {
  agentUrl: string;
  model: string;
  enableThinking?: boolean;
  thinkingLevel?: string;
}

export interface AgentResponseData {
  contextId?: string;
  error?: string;
  role?: string;
  messageId?: string;
  persistedMessageId?: string;
  // Ack that a send_message was rerouted to the already-running turn (steering).
  // Emitted to the sender's sid only; nothing renders — the original stream continues.
  steering?: boolean;
  // Cumulative reply length after this streamed chunk — in Unicode CODE POINTS
  // (Python len), NOT UTF-16 units; used to dedupe live chunks against a resume
  // snapshot after reconnect/reload.
  turnOffset?: number;
  parts?: Array<{ text?: string; kind?: string }>;
  status?: TaskStatusDetails;
  metadata?: Record<string, unknown>;
  artifact?: {
    parts?: Array<{ text?: string; kind?: string }>;
    artifactId?: string;
    contextId?: string;
    role?: string;
    metadata?: Record<string, unknown>;
    extensions?: string[];
  };
  artifacts?: unknown;
  kind?: string;
  id?: string;
  taskId?: string;
  title?: string;
  history?: TaskHistoryEntry[];
  validation_errors?: string[];
  progress?: number;
}

export interface SendMessagePayload {
  id: string;
  conversationId: string;
  message: string;
  sessionId: string;
  metadata?: Record<string, any>;
  contextId?: string;
  // What the backend actually consumes: `uri` (A2A part), plus `s3Url` for
  // server-side processing. (An earlier declaration said `url` — wrong.)
  fileAttachments?: Array<{
    uri: string;
    mimeType: string;
    name: string;
    s3Url?: string;
  }>;
  // Structured payloads riding a (usually silent) message — today exactly one
  // shape: [{ decisions: [...] }] resuming a HITL interrupt.
  dataParts?: Record<string, unknown>[];
}

export interface ClientInitializedData {
  status: 'success' | 'error';
  agent?: AgentInfo;
  error?: string;
  message?: string;
}

/**
 * Resume snapshot for a conversation (multi-replica protocol): current
 * in-flight state + accumulated reply so a reconnecting/late-subscribing
 * client can catch up without losing streamed chunks.
 */
export interface ConversationSnapshotData {
  conversationId: string;
  inFlight: boolean;
  offset: number;
  replyText: string;
  pendingHitl?: AgentResponseData | null;
}

/**
 * Push sent to a conversation's room when the backend changes the conversation
 * ITSELF rather than adding to it — today: the written title and summary that
 * land a moment after the first exchange finishes. Without this the panel would
 * keep showing the first-message placeholder until the next list load.
 */
export interface ConversationUpdatedData {
  conversationId: string;
  title?: string;
  summary?: string;
}

/**
 * The socket.io ACK of a `subscribe_conversation` emit — the same answer the
 * snapshot carries, but delivered as the call's return value, so it arrives in
 * milliseconds even when no snapshot ever follows (the backend rejects the join
 * for a conversation it has never persisted).
 */
export interface SubscribeAck {
  /** The room join was accepted. False = rejected: unknown or foreign conversation. */
  ok: boolean;
  /** A turn is streaming for this conversation right now. */
  inFlight: boolean;
}

/** Read a `subscribe_conversation` ack. `null` = an answer we cannot read (an
 *  older backend, or an unauthenticated emit): the caller must fall back to
 *  waiting for the snapshot. */
export function parseSubscribeAck(raw: unknown): SubscribeAck | null {
  if (!raw || typeof raw !== 'object') return null;
  const data = raw as Record<string, unknown>;
  if (data.status === 'error') return { ok: false, inFlight: false };
  if (data.status === 'success') return { ok: true, inFlight: data.inFlight === true };
  return null;
}
