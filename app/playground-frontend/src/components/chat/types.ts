// Chat application types

export interface User {
  id: string;
  email: string;
  first_name?: string;
  last_name?: string;
  name?: string;
}

export interface Conversation {
  id: string;
  title: string;
  lastMessage: string;
  timestamp: Date;
  lastUpdatedRaw?: string;
  status: 'active' | 'archived';
  hasActiveTasks: boolean;
}

export interface Message {
  id: string;
  conversationId: string;
  type: 'user' | 'agent' | 'task';
  content: string;
  timestamp: Date;
}

export interface TaskHistoryEntry {
  contextId?: string | null;
  kind?: string;
  messageId?: string | null;
  parts?: Array<{ kind?: string; text?: string }>;
  role?: string;
  taskId?: string | null;
}

export interface Task {
  id: string;
  conversationId: string;
  title: string;
  status: TaskStatus;
  statusDetails?: TaskStatusDetails | null;
  contextId?: string | null;
  history: TaskHistoryEntry[];
  validationErrors: string[];
  progress: number;
  timestamp: Date;
  result?: string | null;
  source?: unknown;
  taskId?: string | null;
}

export type TaskStatus =
  | 'pending'
  | 'running'
  | 'in_progress'
  | 'completed'
  | 'failed'
  | 'succeeded'
  | 'cancelled'
  | 'unknown';

export interface TaskStatusDetails {
  state?: string;
  label?: string;
  message?: {
    parts?: Array<{ text?: string }>;
    contextId?: string;
    messageId?: string;
    kind?: string;
    role?: string;
    taskId?: string;
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
}

export interface AgentResponseData {
  contextId?: string;
  error?: string;
  role?: string;
  parts?: Array<{ text?: string; kind?: string }>;
  status?: TaskStatusDetails;
  artifact?: {
    parts?: Array<{ text?: string; kind?: string }>;
    artifactId?: string;
    contextId?: string;
    role?: string;
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
  metadata?: Record<string, string>;
  contextId?: string;
}

export interface ClientInitializedData {
  status: 'success' | 'error';
  agent?: AgentInfo;
  error?: string;
  message?: string;
}
