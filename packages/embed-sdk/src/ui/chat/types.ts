// Chat application types

// A2A extension URIs + socket wire types — single source of truth lives in the
// Embed SDK core, so the widget and console agree on the protocol. Re-exported
// here so existing `../types` importers are unchanged.
export {
  ACTIVITY_LOG_EXT,
  WORK_PLAN_EXT,
  INTERMEDIATE_OUTPUT_EXT,
  FEEDBACK_REQUEST_EXT,
  HITL_EXT,
  CLIENT_ACTION_EXT,
} from '../../core';
export type {
  AgentInfo,
  AgentResponseData,
  ClientInitializedData,
  SendMessagePayload,
  Settings,
  TaskHistoryEntry,
  TaskStatusDetails,
} from '../../core';

import type { TaskHistoryEntry, TaskStatusDetails } from '../../core';

export interface PendingInterrupt {
  conversationId: string;
  taskId?: string;
  toolName: string;
  reason: string;
  actionRequests?: Array<{ name: string; args: Record<string, unknown>; description?: string }>;
  reviewConfigs?: Array<{ action_name: string; allowed_decisions: string[] }>;
}

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
  /** Set when the conversation was created by an embedded widget scoped to this
   *  sub-agent. Outside that host (e.g. the console) it renders read-only: its
   *  turns assume a live host page with registered client objects. */
  embeddedSubAgentId?: string;
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

