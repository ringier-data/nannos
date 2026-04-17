// Chat application types

// A2A extension URIs for classifying streaming events
export const ACTIVITY_LOG_EXT = 'urn:nannos:a2a:activity-log:1.0';
export const WORK_PLAN_EXT = 'urn:nannos:a2a:work-plan:1.0';
export const INTERMEDIATE_OUTPUT_EXT = 'urn:nannos:a2a:intermediate-output:1.0';

export interface TodoItem {
  name: string;
  state: 'submitted' | 'working' | 'completed' | 'failed';
  source?: string;
  target?: string;
}

// Unified timeline event for chronological display
export type TimelineEvent =
  | { type: 'todo_snapshot'; timestamp: Date; todos: TodoItem[] }  // Progressive widget (show latest)
  | { type: 'status'; timestamp: Date; message: string; source?: string }  // Discrete event
  | { type: 'thought_start'; timestamp: Date; agent_name: string }  // Marks start of sub-agent work
  | { type: 'thought_end'; timestamp: Date; agent_name: string; content: string; complete: boolean };  // Complete thought

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
  // Unified chronological timeline of all events
  timeline?: TimelineEvent[];
  // Control whether to show MessageCard (false for timeline-only messages)
  showMessageCard?: boolean;
  parts?: Array<{
    kind: string;
    text?: string;
    file?: {
      uri: string;
      mimeType?: string;
      name?: string;
    };
  }>;
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
    parts?: Array<{ text?: string; kind?: string; data?: unknown; media_type?: string }>;
    contextId?: string;
    messageId?: string;
    kind?: string;
    role?: string;
    taskId?: string;
    extensions?: string[];
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
