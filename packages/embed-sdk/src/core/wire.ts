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
  // Cumulative reply length after this streamed chunk; used to dedupe live chunks
  // against a resume snapshot after reconnect/reload.
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
  fileAttachments?: Array<{
    url: string;
    mimeType: string;
    name: string;
  }>;
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
