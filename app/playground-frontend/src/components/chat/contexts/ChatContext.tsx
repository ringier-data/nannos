import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { toast } from 'sonner';
import type { AgentResponseData, Conversation, Message, Settings, Task, TaskHistoryEntry, TodoItem, TimelineEvent } from '../types';
import { ACTIVITY_LOG_EXT, WORK_PLAN_EXT, INTERMEDIATE_OUTPUT_EXT } from '../types';
import { useSocket } from './SocketContext';
import { useSessionId } from '../hooks/useLocalStorage';
import { extractPartTexts, generateUUID, getTaskState, isTaskComplete, shouldDisplayMessageParts } from '../utils';
import {
  getAdminModeFromStorage,
  getImpersonatedUserIdFromStorage,
  ADMIN_MODE_HEADER,
  IMPERSONATE_USER_HEADER,
} from '../../../api/apiInstanceConfig';
import { getCurrentUserSettingsApiV1AuthMeSettingsGetOptions } from '@/api/generated/@tanstack/react-query.gen';
import type { UploadedFileInfo } from '@/api/generated';
import { config } from '@/config';

interface ChatContextType {
  // State
  conversations: Conversation[];
  activeConversationId: string | null;
  messages: Message[];
  tasks: Task[];
  settings: Settings | null;
  userSettings: { preferred_model?: string | null; enable_thinking?: boolean | null; thinking_level?: string | null } | null;
  isLoadingConversations: boolean;
  isLoadingMessages: boolean;
  isConnected: boolean;
  isWaiting: boolean;
  streamingMessage: string | null;
  liveWorkingSteps: TodoItem[];
  liveSubagentThoughts: Array<{agent_name: string; content: string; complete: boolean}>;
  liveStatusHistory: Array<{timestamp: Date; message: string}>;
  liveTimeline: TimelineEvent[];

  // Actions
  createConversation: () => void;
  selectConversation: (id: string) => void;
  sendMessage: (
    content: string, 
    files?: Array<Pick<UploadedFileInfo, 'uri' | 'mimeType' | 'name' | 's3Url'>>
  ) => void;
  interruptTask: () => void;
  updateSettings: (settings: Settings) => Promise<boolean>;
  loadConversations: () => Promise<void>;
}

const ChatContext = createContext<ChatContextType | undefined>(undefined);

/**
 * Playground mode configuration for testing specific sub-agent versions.
 */
export interface PlaygroundMode {
  /** The version hash of the sub-agent config being tested */
  subAgentConfigHash: string;
  /** Human-readable name for display */
  subAgentName: string;
}

interface ChatProviderProps {
  children: ReactNode;
  /** When set, filters conversations by this config hash and tags new ones */
  playgroundMode?: PlaygroundMode;
}

// Helper to build unified timeline from thoughts and status history
// Todos are kept in sticky widget only, not in timeline
const buildTimeline = (
  thoughts: Array<{ agent_name: string; content: string; complete?: boolean }> | undefined,
  history: Array<{ timestamp: Date; message: string; source?: string }> | undefined,
  baseTimestamp: Date
): TimelineEvent[] => {
  const events: TimelineEvent[] = [];
  
  // Add status history items (they have individual timestamps)
  if (history && history.length > 0) {
    history.forEach(item => {
      events.push({ type: 'status', timestamp: item.timestamp, message: item.message, ...(item.source && { source: item.source }) });
    });
  }
  
  // Add thoughts (use base timestamp, mark start and end)
  if (thoughts && thoughts.length > 0) {
    thoughts.forEach(thought => {
      events.push({ type: 'thought_start', timestamp: baseTimestamp, agent_name: thought.agent_name });
      events.push({ type: 'thought_end', timestamp: baseTimestamp, agent_name: thought.agent_name, content: thought.content, complete: thought.complete ?? true });
    });
  }
  
  // Sort chronologically
  return events.sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());
};

// Helper to reconstruct timeline from saved message data
const reconstructTimelineFromMessage = (msg: Record<string, unknown>): TimelineEvent[] => {
  const events: TimelineEvent[] = [];
  const timestamp = msg.created_at ? new Date(msg.created_at as string) : new Date();
  
  // Parse raw_payload if available (contains full response_data)
  let responseData: Record<string, unknown> | null = null;
  if (typeof msg.raw_payload === 'string' && msg.raw_payload) {
    try {
      responseData = JSON.parse(msg.raw_payload);
    } catch (e) {
      console.warn('Failed to parse raw_payload:', e);
    }
  }
  
  const metadata = (msg.metadata || (responseData?.metadata)) as Record<string, unknown> | undefined;
  const kind = (msg.kind || responseData?.kind) as string | undefined;
  
  // Extract activity-log events (status message with activity-log extension)
  const statusObj = responseData?.status as Record<string, unknown> | undefined;
  const statusMsg = statusObj?.message as Record<string, unknown> | undefined;
  const messageExtensions = (statusMsg?.extensions || []) as string[];
  if (messageExtensions.includes(ACTIVITY_LOG_EXT)) {
    // Try to get message from status object or from parts
    let message = '';
    let source: string | undefined;
    
    if (responseData) {
      const status = responseData.status as Record<string, unknown> | undefined;
      if (status?.message) {
        // status.message can be either a string or a nested Message object with parts
        if (typeof status.message === 'string') {
          message = status.message;
        } else if (typeof status.message === 'object' && status.message !== null) {
          // Extract text from nested message parts
          const nestedParts = (status.message as Record<string, unknown>).parts as Array<{ kind?: string; text?: string }> | undefined;
          if (nestedParts && Array.isArray(nestedParts)) {
            message = nestedParts.map(p => p.text || '').join(' ').trim();
          }
        }
      }
      // Extract source from status message metadata (for sub-agent attribution)
      const msgMetadata = statusMsg?.metadata as Record<string, unknown> | undefined;
      if (msgMetadata?.source && typeof msgMetadata.source === 'string') {
        source = msgMetadata.source;
      } else if (metadata?.source && typeof metadata.source === 'string') {
        source = metadata.source;
      }
    }
    
    if (!message) {
      // Fallback: extract from parts
      const parts = msg.parts as Array<{ kind?: string; text?: string }> | undefined;
      if (parts && parts.length > 0) {
        message = parts.map(p => p.text || '').join(' ').trim();
      }
    }
    
    if (message) {
      events.push({ type: 'status', timestamp, message, ...(source && { source }) });
    }
  }
  
  // Extract intermediate output (sub-agent thoughts) via artifact extensions
  if (kind === 'artifact-update' && responseData) {
    const artifact = responseData.artifact as Record<string, unknown> | undefined;
    const artifactExtensions = (artifact?.extensions || []) as string[];
    const artifactMetadata = artifact?.metadata as Record<string, unknown> | undefined;
    
    if (artifactExtensions.includes(INTERMEDIATE_OUTPUT_EXT)) {
      const agentName = (artifactMetadata?.agent_name || 'sub-agent') as string;

      // Extract thought content from artifact parts
      let content = '';
      const parts = artifact?.parts as Array<{ kind?: string; text?: string }> | undefined;
      if (parts && parts.length > 0) {
        content = parts.map(p => p.text || '').join('').trim();
      }

      if (content) {
        events.push({ type: 'thought_end', timestamp, agent_name: agentName, content, complete: true });
      }
    }
  }
  
  return events;
};

export function ChatProvider({ children, playgroundMode }: ChatProviderProps) {
  const sessionId = useSessionId();
  const location = useLocation();
  const locationRef = useRef(location);
  const navigate = useNavigate();
  const navigateRef = useRef(navigate);
  const { isConnected, isSocketReady, initializeClient, sendMessage: socketSendMessage, cancelTask, onAgentResponse } = useSocket();

  // Keep refs in sync for use inside event handler closures
  useEffect(() => {
    locationRef.current = location;
  }, [location]);
  useEffect(() => {
    navigateRef.current = navigate;
  }, [navigate]);

  // Load user settings to use as defaults
  const { data: userSettingsData } = useQuery({
    ...getCurrentUserSettingsApiV1AuthMeSettingsGetOptions(),
  });
  
  const userSettings = userSettingsData?.data;

  // State
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [messagesMap, setMessagesMap] = useState<Map<string, Message[]>>(new Map());
  const [tasksMap, setTasksMap] = useState<Map<string, Task[]>>(new Map());
  const [contextIdsMap, setContextIdsMap] = useState<Map<string, string>>(new Map());
  const [waitingMap, setWaitingMap] = useState<Map<string, boolean>>(new Map());
  const [streamingMap, setStreamingMap] = useState<Map<string, string>>(new Map());
  const [workingStepsMap, setWorkingStepsMap] = useState<Map<string, TodoItem[]>>(new Map());
  const [subagentThoughtsMap, setSubagentThoughtsMap] = useState<Map<string, Array<{agent_name: string; content: string; complete: boolean}>>>(new Map());
  const [statusHistoryMap, setStatusHistoryMap] = useState<Map<string, Array<{timestamp: Date; message: string; source?: string}>>>(new Map());
  const workingStepsMapRef = useRef<Map<string, TodoItem[]>>(new Map());
  const streamingMapRef = useRef<Map<string, string>>(new Map());
  const subagentThoughtsMapRef = useRef<Map<string, Array<{agent_name: string; content: string; complete: boolean}>>>(new Map());
  const statusHistoryMapRef = useRef<Map<string, Array<{timestamp: Date; message: string; source?: string}>>>(new Map());
  const [isLoadingConversations, setIsLoadingConversations] = useState(false);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);
  // Settings are ephemeral and not persisted to localStorage
  const [settings, setSettings] = useState<Settings | null>(null);

  const messageCounterRef = useRef(0);
  const taskCounterRef = useRef(0);
  const playgroundModeRef = useRef(playgroundMode);
  const activeConversationIdRef = useRef(activeConversationId);
  const pendingMessagesRef = useRef<Map<string, string>>(new Map()); // messageId → conversationId

  // Keep ref in sync
  useEffect(() => {
    activeConversationIdRef.current = activeConversationId;
  }, [activeConversationId]);

  // Derived state
  const messages = activeConversationId ? messagesMap.get(activeConversationId) || [] : [];
  const tasks = activeConversationId ? tasksMap.get(activeConversationId) || [] : [];
  const isWaiting = activeConversationId ? waitingMap.get(activeConversationId) === true : false;
  const streamingMessage = activeConversationId ? streamingMap.get(activeConversationId) ?? null : null;
  const liveWorkingSteps = activeConversationId ? workingStepsMap.get(activeConversationId) || [] : [];
  const liveSubagentThoughts = activeConversationId ? subagentThoughtsMap.get(activeConversationId) || [] : [];
  const liveStatusHistory = activeConversationId ? statusHistoryMap.get(activeConversationId) || [] : [];
  
  // Build live unified timeline during streaming (maintains chronological order)
  const liveTimeline = activeConversationId 
    ? buildTimeline(liveSubagentThoughts, liveStatusHistory, new Date())
    : [];

  // Helper to add a message
  const addMessage = useCallback((conversationId: string, message: Message) => {
    setMessagesMap((prev) => {
      const newMap = new Map(prev);
      const existing = newMap.get(conversationId) || [];
      newMap.set(conversationId, [...existing, message]);
      return newMap;
    });
  }, []);

  // Helper to show toast notification for new agent messages
  // Only shows when user is NOT actively viewing the conversation
  const showMessageToast = useCallback((conversationId: string, content: string) => {
    if (!content.trim()) return;
    const isOnChatPage = locationRef.current.pathname === '/app/chat';
    const isViewingThisConversation = activeConversationIdRef.current === conversationId;
    if (isOnChatPage && isViewingThisConversation) return;

    const preview = content.slice(0, 60) + (content.length > 60 ? '...' : '');
    const convId = conversationId;
    toast.info('New message', {
      description: preview,
      duration: 5000,
      action: {
        label: 'View',
        onClick: () => {
          setActiveConversationId(convId);
          navigateRef.current('/app/chat');
        },
      },
    });
  }, []);

  // Helper to add/update a task
  const addOrUpdateTask = useCallback((conversationId: string, task: Task) => {
    setTasksMap((prev) => {
      const newMap = new Map(prev);
      const existing = newMap.get(conversationId) || [];
      const existingIndex = existing.findIndex((t) => t.id === task.id);
      if (existingIndex >= 0) {
        const updated = [...existing];
        updated[existingIndex] = task;
        newMap.set(conversationId, updated);
      } else {
        newMap.set(conversationId, [...existing, task]);
      }
      return newMap;
    });
  }, []);

  // Helper to update conversation
  const updateConversation = useCallback((id: string, updates: Partial<Conversation>) => {
    setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, ...updates } : c)));
  }, []);

  // Handle agent responses
  useEffect(() => {
    const unsubscribe = onAgentResponse((data: AgentResponseData) => {
      // Debug: log all incoming events
      if (data.role === 'agent' && data.kind === 'status-update') {
        console.log('[SOCKET EVENT] status-update:', data);
      }
      
      // Route to the correct conversation using contextId, pending message lookup, or active conversation
      const resolvedConversationId =
        data.contextId ??
        (data.id ? pendingMessagesRef.current.get(data.id) : undefined) ??
        activeConversationIdRef.current;

      if (!resolvedConversationId) {
        console.warn('Received response but no conversation could be resolved');
        return;
      }

      // Save context_id
      if (data.contextId) {
        setContextIdsMap((prev) => {
          const newMap = new Map(prev);
          newMap.set(resolvedConversationId, data.contextId!);
          return newMap;
        });
      }

      // Handle work-plan (todo snapshot) — detect via extensions on status.message
      const statusMessage = data.status?.message;
      const statusExtensions = (statusMessage?.extensions || []) as string[];
      if (statusExtensions.includes(WORK_PLAN_EXT) && Array.isArray(statusMessage?.parts)) {
        // Extract todos from DataPart (part with kind === 'data')
        const dataPart = statusMessage.parts.find((p: any) => p.kind === 'data' || p.data);
        const todosData = dataPart?.data as Record<string, unknown> | undefined;
        const incomingTodos = (todosData?.todos || []) as TodoItem[];
        if (incomingTodos.length > 0) {
        const wsMap = workingStepsMapRef.current;
        const existing = wsMap.get(resolvedConversationId) || [];

        // Determine which source(s) this snapshot covers
        const incomingSources = new Set(incomingTodos.map((t) => t.source || ''));

        // Keep existing todos from OTHER sources
        const retained = existing.filter((t) => !incomingSources.has(t.source || ''));

        // Merge: orchestrator todos (no source) first, then sub-agent groups
        const merged = [...retained, ...incomingTodos];
        merged.sort((a, b) => {
          const aSource = a.source || '';
          const bSource = b.source || '';
          if (!aSource && bSource) return -1;
          if (aSource && !bSource) return 1;
          return aSource.localeCompare(bSource);
        });

        wsMap.set(resolvedConversationId, merged);
        setWorkingStepsMap(new Map(wsMap));
        return;
        }
      }

      // Handle streaming artifact chunks (A2A artifact-append pattern)
      if (data.kind === 'artifact-update' && data.artifact?.parts) {
        const parts = data.artifact.parts;
        if (Array.isArray(parts)) {
          const text = extractPartTexts(parts).join('');
          if (text) {
            // Classify artifact via extensions array (A2A 1.0.0)
            const artifactExtensions = ((data.artifact as any)?.extensions || []) as string[];
            const artifactMetadata = (data.artifact as any)?.metadata || {};
            const isIntermediateOutput = artifactExtensions.includes(INTERMEDIATE_OUTPUT_EXT);
            const agentName = (artifactMetadata.agent_name as string) || 'sub-agent';
            
            if (isIntermediateOutput) {
              // Accumulate sub-agent thoughts separately
              const existingThoughts = subagentThoughtsMapRef.current.get(resolvedConversationId) || [];
              const lastThought = existingThoughts[existingThoughts.length - 1];
              
              if (lastThought && lastThought.agent_name === agentName) {
                // Append to existing thought from same agent
                lastThought.content += text;
              } else {
                // New thought from different agent — mark all previous as complete
                existingThoughts.forEach(t => { t.complete = true; });
                existingThoughts.push({ agent_name: agentName, content: text, complete: false });
              }
              
              subagentThoughtsMapRef.current.set(resolvedConversationId, existingThoughts);
              setSubagentThoughtsMap(new Map(subagentThoughtsMapRef.current)); // Trigger re-render
            } else {
              // Orchestrator's final response chunks — thinking is done for all agents
              const thoughts = subagentThoughtsMapRef.current.get(resolvedConversationId);
              if (thoughts?.some(t => !t.complete)) {
                thoughts.forEach(t => { t.complete = true; });
                setSubagentThoughtsMap(new Map(subagentThoughtsMapRef.current));
              }
              const existing = streamingMapRef.current.get(resolvedConversationId) || '';
              streamingMapRef.current.set(resolvedConversationId, existing + text);
              setStreamingMap(new Map(streamingMapRef.current));
            }
          }
        }
        // Don't finalize here - let the completion status handler finalize the stream
        return;
      }

      // Capture activity-log events for linear display (similar to VSCode Copilot chat)
      // Detected via extensions on status.message (A2A 1.0.0)
      if (data.kind === 'status-update') {
        const statusMsg = data.status?.message;
        const exts = ((statusMsg as any)?.extensions || []) as string[];
        if (exts.includes(ACTIVITY_LOG_EXT)) {
        // Extract text from message parts (status.message.parts)
        if (statusMsg && Array.isArray(statusMsg.parts)) {
          const text = extractPartTexts(statusMsg.parts).join('');
          
          if (text && text.trim()) {
            // Extract source attribution from message metadata (sub-agent name)
            const msgMetadata = (statusMsg as any)?.metadata as Record<string, unknown> | undefined;
            const source = msgMetadata?.source && typeof msgMetadata.source === 'string' ? msgMetadata.source : undefined;
            // This is an activity-log event (tool call, delegation) - add to history
            const history = statusHistoryMapRef.current.get(resolvedConversationId) || [];
            history.push({
              timestamp: new Date(),
              message: text,
              ...(source && { source })
            });
            statusHistoryMapRef.current.set(resolvedConversationId, history);
            setStatusHistoryMap(new Map(statusHistoryMapRef.current));
            return; // Don't process as regular status update
          }
        }
        }
      }

      // Clear streaming buffer only on final message responses (not status updates)
      // Status updates can happen during streaming, so we shouldn't clear the buffer
      if (data.role === 'agent' && Array.isArray(data.parts) && shouldDisplayMessageParts(data.parts)) {
        if (streamingMapRef.current.has(resolvedConversationId)) {
          streamingMapRef.current.delete(resolvedConversationId);
          setStreamingMap(new Map(streamingMapRef.current));
        }
      }

      // Handle error
      if (data.error) {
        messageCounterRef.current++;
        const errorMsg: Message = {
          id: `msg-${messageCounterRef.current}`,
          conversationId: resolvedConversationId,
          type: 'agent',
          content: `Error: ${data.error}`,
          timestamp: new Date(),
        };
        addMessage(resolvedConversationId, errorMsg);
        updateConversation(resolvedConversationId, { lastMessage: errorMsg.content });
        // Clean up pending message tracking
        if (data.id) pendingMessagesRef.current.delete(data.id);
        setWaitingMap((prev) => { const m = new Map(prev); m.delete(resolvedConversationId); return m; });
        workingStepsMapRef.current.delete(resolvedConversationId);
        setWorkingStepsMap(new Map(workingStepsMapRef.current));
        return;
      }

      // Handle message response
      if (data.role === 'agent' && Array.isArray(data.parts)) {
        if (shouldDisplayMessageParts(data.parts)) {
          const text = extractPartTexts(data.parts).join('\n');
          const steps = workingStepsMapRef.current.get(resolvedConversationId);
          messageCounterRef.current++;
          const timestamp = new Date();
          const agentMsg: Message = {
            id: `msg-${messageCounterRef.current}`,
            conversationId: resolvedConversationId,
            type: 'agent',
            content: text,
            timestamp,
            ...(steps && steps.length > 0 && { workingSteps: steps }),
            timeline: buildTimeline(undefined, undefined, timestamp),
          };
          addMessage(resolvedConversationId, agentMsg);
          updateConversation(resolvedConversationId, {
            lastMessage: text.slice(0, 50),
            timestamp: new Date(),
          });
          // Keep working steps visible in sticky widget (don't delete)
          // Clear waiting state and pending message tracking
          if (data.id) pendingMessagesRef.current.delete(data.id);
          setWaitingMap((prev) => { const m = new Map(prev); m.delete(resolvedConversationId); return m; });
        }
        return;
      }

      // Handle task status update
      if (data.status) {
        const normalizedStatus = getTaskState(data.status?.state);

        // Finalize streaming message when task completes or fails
        let finalizedFromStream = false;
        if (normalizedStatus === 'completed' || normalizedStatus === 'failed' || normalizedStatus === 'canceled') {
          const streamedText = streamingMapRef.current.get(resolvedConversationId);
          const thoughts = subagentThoughtsMapRef.current.get(resolvedConversationId);
          // Mark all thoughts as complete — task is done
          if (thoughts) thoughts.forEach(t => { t.complete = true; });
          const steps = workingStepsMapRef.current.get(resolvedConversationId);
          const history = statusHistoryMapRef.current.get(resolvedConversationId);

          const finalizeMessage = (content: string) => {
            messageCounterRef.current++;
            const timestamp = new Date();
            const agentMsg: Message = {
              id: `msg-${messageCounterRef.current}`,
              conversationId: resolvedConversationId,
              type: 'agent',
              content,
              timestamp,
              ...(steps && steps.length > 0 && { workingSteps: steps }),
              ...(thoughts && thoughts.length > 0 && { subagentThoughts: thoughts }),
              ...(history && history.length > 0 && { statusHistory: history }),
              timeline: buildTimeline(thoughts, history, timestamp),
            };
            addMessage(resolvedConversationId, agentMsg);
            updateConversation(resolvedConversationId, {
              lastMessage: content.slice(0, 50),
              timestamp: new Date(),
            });
            // Keep todos visible in sticky widget (don't delete workingStepsMapRef)
            // Clear only thoughts and history
            subagentThoughtsMapRef.current.delete(resolvedConversationId);
            setSubagentThoughtsMap(new Map(subagentThoughtsMapRef.current));
            statusHistoryMapRef.current.delete(resolvedConversationId);
            setStatusHistoryMap(new Map(statusHistoryMapRef.current));

            showMessageToast(resolvedConversationId, content);
          };

          if (streamedText && streamedText.trim()) {
            // Orchestrator streamed its own response token-by-token
            finalizeMessage(streamedText);
            finalizedFromStream = true;
          }

          // Always clear streaming buffer and waiting indicator on completion
          streamingMapRef.current.delete(resolvedConversationId);
          setStreamingMap(new Map(streamingMapRef.current));
          setWaitingMap((prev) => {
            const m = new Map(prev);
            m.delete(resolvedConversationId);
            return m;
          });
        }

        // Extract nested message if present (skip if already finalized from streamed content)
        const nestedMsg = data.status.message;
        if (!finalizedFromStream && nestedMsg && Array.isArray(nestedMsg.parts) && shouldDisplayMessageParts(nestedMsg.parts)) {
          const text = extractPartTexts(nestedMsg.parts).join('\n');

          if (normalizedStatus === 'working') {
            // Accumulate as a working step (non-todo status messages)
            const wsMap = workingStepsMapRef.current;
            const existing = wsMap.get(resolvedConversationId) || [];
            wsMap.set(resolvedConversationId, [...existing, { name: text, state: 'completed' as const }]);
            setWorkingStepsMap(new Map(wsMap));
          } else {
            // Terminal/final state — create message bubble with the full accumulated timeline
            const steps = workingStepsMapRef.current.get(resolvedConversationId);
            const thoughts = subagentThoughtsMapRef.current.get(resolvedConversationId);
            const history = statusHistoryMapRef.current.get(resolvedConversationId);
            messageCounterRef.current++;
            const timestamp = new Date();
            const agentMsg: Message = {
              id: `msg-${messageCounterRef.current}`,
              conversationId: resolvedConversationId,
              type: 'agent',
              content: text,
              timestamp,
              ...(steps && steps.length > 0 && { workingSteps: steps }),
              ...(thoughts && thoughts.length > 0 && { subagentThoughts: thoughts }),
              ...(history && history.length > 0 && { statusHistory: history }),
              timeline: buildTimeline(thoughts, history, timestamp),
            };
            addMessage(resolvedConversationId, agentMsg);
            updateConversation(resolvedConversationId, {
              lastMessage: text.slice(0, 50),
              timestamp: new Date(),
            });
            // Keep todos visible in sticky widget (don't delete workingStepsMapRef)
            // Clear only thoughts and history so live timeline/spinner clears
            subagentThoughtsMapRef.current.delete(resolvedConversationId);
            setSubagentThoughtsMap(new Map(subagentThoughtsMapRef.current));
            statusHistoryMapRef.current.delete(resolvedConversationId);
            setStatusHistoryMap(new Map(statusHistoryMapRef.current));

            showMessageToast(resolvedConversationId, text);
          }
        }

        // Update task
        const taskId = data.id || `task-${taskCounterRef.current++}`;
        const existingTasks = tasksMap.get(resolvedConversationId) || [];
        const existingTask = existingTasks.find((t) => t.id === taskId);

        const historyEntries: TaskHistoryEntry[] = Array.isArray(data.history) ? data.history : [];
        const validationErrors = Array.isArray(data.validation_errors) ? data.validation_errors : [];
        const progressValue =
          typeof data.progress === 'number'
            ? data.progress
            : typeof data.status?.progress === 'number'
              ? data.status.progress
              : null;
        const contextId = data.contextId || historyEntries[0]?.contextId || existingTask?.contextId || null;

        const task: Task = existingTask
          ? {
              ...existingTask,
              status: normalizedStatus as Task['status'],
              statusDetails: data.status,
              title: data.title || existingTask.title,
              contextId: contextId || existingTask.contextId,
              history: historyEntries.length ? historyEntries : existingTask.history,
              validationErrors: validationErrors.length ? validationErrors : existingTask.validationErrors,
              progress: typeof progressValue === 'number' ? progressValue : existingTask.progress,
              taskId: data.taskId || existingTask.taskId,
              timestamp: new Date(),
            }
          : {
              id: taskId,
              conversationId: resolvedConversationId,
              title: data.title || 'Task',
              status: normalizedStatus as Task['status'],
              statusDetails: data.status,
              contextId,
              history: historyEntries,
              validationErrors,
              progress: typeof progressValue === 'number' ? progressValue : 0,
              timestamp: new Date(),
              result: null,
              source: data,
              taskId: data.taskId || null,
            };

        addOrUpdateTask(resolvedConversationId, task);

        // Update conversation active tasks status
        const allTasks = tasksMap.get(resolvedConversationId) || [];
        const hasActiveTasks = allTasks.some((t) => !isTaskComplete(t.status));
        updateConversation(resolvedConversationId, { hasActiveTasks });

        // Clean up pending message tracking on terminal states
        if (isTaskComplete(normalizedStatus)) {
          if (data.id) pendingMessagesRef.current.delete(data.id);
          setWaitingMap((prev) => { const m = new Map(prev); m.delete(resolvedConversationId); return m; });
          // Keep todos visible in sticky widget (don't delete workingStepsMapRef)
        }
        return;
      }

      // Handle artifact update
      if (data.artifact || data.kind === 'artifact-update') {
        const art =
          data.artifact ||
          (Array.isArray(data.artifacts) ? (data.artifacts as { parts?: { text?: string }[] }[])[0] : null);
        if (art && Array.isArray(art.parts) && shouldDisplayMessageParts(art.parts)) {
          const text = extractPartTexts(art.parts).join('\n');
          messageCounterRef.current++;
          const agentMsg: Message = {
            id: `msg-${messageCounterRef.current}`,
            conversationId: resolvedConversationId,
            type: 'agent',
            content: text,
            timestamp: new Date(),
          };
          addMessage(resolvedConversationId, agentMsg);
          updateConversation(resolvedConversationId, {
            lastMessage: text.slice(0, 50),
            timestamp: new Date(),
          });
        }

        // Update task with artifact
        const taskId = data.id || `task-${taskCounterRef.current++}`;
        const existingTasks = tasksMap.get(resolvedConversationId) || [];
        const existingTask = existingTasks.find((t) => t.id === taskId);

        const task: Task = existingTask
          ? {
              ...existingTask,
              result: JSON.stringify(data.artifacts || data.artifact, null, 2),
              status: 'completed',
              statusDetails: { state: 'completed' },
              progress: 100,
              timestamp: new Date(),
            }
          : {
              id: taskId,
              conversationId: resolvedConversationId,
              title: 'Task Result',
              status: 'completed',
              statusDetails: { state: 'completed' },
              contextId: null,
              history: [],
              validationErrors: [],
              progress: 100,
              timestamp: new Date(),
              result: JSON.stringify(data.artifacts || data.artifact, null, 2),
              source: data,
              taskId: null,
            };

        addOrUpdateTask(resolvedConversationId, task);
        return;
      }

      // Unknown response - display as JSON
      messageCounterRef.current++;
      const genericMsg: Message = {
        id: `msg-${messageCounterRef.current}`,
        conversationId: resolvedConversationId,
        type: 'agent',
        content: JSON.stringify(data, null, 2),
        timestamp: new Date(),
      };
      addMessage(resolvedConversationId, genericMsg);
    });

    return unsubscribe;
  }, [onAgentResponse, addMessage, addOrUpdateTask, updateConversation, tasksMap]);

  // Load conversations from backend
  const loadConversations = useCallback(async () => {
    setIsLoadingConversations(true);
    try {
      const effectiveAgentUrl = settings?.agentUrl || null;
      const url = new URL('/api/v1/conversations/', window.location.origin);
      url.searchParams.set('limit', '50');
      if (effectiveAgentUrl) url.searchParams.set('agent_url', effectiveAgentUrl);
      // In playground mode, filter by sub_agent_config_hash
      if (playgroundModeRef.current?.subAgentConfigHash) {
        url.searchParams.set('sub_agent_config_hash', playgroundModeRef.current.subAgentConfigHash);
      } else {
        // In main chat, exclude playground conversations
        url.searchParams.set('exclude_playground', 'true');
      }

      // Inject impersonation headers if active
      const headers: HeadersInit = {};
      const impersonatedUserId = getImpersonatedUserIdFromStorage();
      if (impersonatedUserId) {
        headers[IMPERSONATE_USER_HEADER] = impersonatedUserId;
        headers[ADMIN_MODE_HEADER] = 'true'; // Force admin mode when impersonating
      } else {
        const adminMode = getAdminModeFromStorage();
        if (adminMode) {
          headers[ADMIN_MODE_HEADER] = 'true';
        }
      }

      const resp = await fetch(url.toString(), { credentials: 'include', headers });
      if (!resp.ok) {
        throw new Error(`Failed to load conversations (status=${resp.status})`);
      }
      const data = await resp.json();

      const raw = Array.isArray(data.items) ? data.items : Array.isArray(data.conversations) ? data.conversations : [];

      const mapped: Conversation[] = raw.map((c: Record<string, unknown>) => {
        const id = (c.id || c.conversation_id || c.conversationId) as string;
        const title = (c.title || (c.metadata as Record<string, unknown>)?.title || 'Conversation') as string;
        const lastMessage = (c.last_message || c.lastMessage || '') as string;
        const ts =
          c.last_message_at ||
          c.lastMessageAt ||
          c.last_updated ||
          c.lastUpdated ||
          c.updated_at ||
          c.started_at ||
          c.created_at ||
          null;
        const timestamp = ts ? new Date(ts as string) : new Date();
        return {
          id,
          title,
          lastMessage,
          timestamp,
          lastUpdatedRaw: (ts as string) || undefined,
          status: (c.status as 'active' | 'archived') || 'active',
          hasActiveTasks: !!c.has_active_tasks,
        };
      });

      setConversations(mapped);

      // Cache context ids
      if (Array.isArray(data.items)) {
        const newContextIds = new Map(contextIdsMap);
        data.items.forEach((c: Record<string, unknown>) => {
          if (c.context_id) {
            newContextIds.set(c.id as string, c.context_id as string);
          }
        });
        setContextIdsMap(newContextIds);
      }

      // Auto-select first conversation if none selected
      if (!activeConversationId && mapped.length > 0) {
        setActiveConversationId(mapped[0].id);
      }
    } catch (e) {
      console.error('loadConversations failed', e);
    } finally {
      setIsLoadingConversations(false);
    }
  }, [settings?.agentUrl, activeConversationId, contextIdsMap]);

  // Load messages for a conversation
  const loadMessages = useCallback(async (conversationId: string) => {
    setIsLoadingMessages(true);
    try {
      const url = new URL(`/api/v1/messages/${encodeURIComponent(conversationId)}`, window.location.origin);
      url.searchParams.set('limit', '100');

      // Inject impersonation headers if active
      const headers: HeadersInit = {};
      const impersonatedUserId = getImpersonatedUserIdFromStorage();
      if (impersonatedUserId) {
        headers[IMPERSONATE_USER_HEADER] = impersonatedUserId;
        headers[ADMIN_MODE_HEADER] = 'true'; // Force admin mode when impersonating
      } else {
        const adminMode = getAdminModeFromStorage();
        if (adminMode) {
          headers[ADMIN_MODE_HEADER] = 'true';
        }
      }

      const resp = await fetch(url.toString(), { credentials: 'include', headers });
      if (!resp.ok) {
        // 404 means conversation doesn't exist yet (brand new) - that's okay, just skip loading
        if (resp.status === 404) {
          return;
        }
        throw new Error(`Failed to load messages (status=${resp.status})`);
      }
      const data = await resp.json();

      const raw = Array.isArray(data.items) ? data.items : Array.isArray(data.messages) ? data.messages : [];

      const mapped: Message[] = raw
        .map((m: Record<string, unknown>) => {
          const partArray = m.parts as Array<{ kind?: string; text?: string; file?: { uri: string; mimeType?: string; name?: string } }> | undefined;
          if (partArray && !shouldDisplayMessageParts(partArray) && typeof m.content !== 'string') {
            return null;
          }
          const id = (m.id || m.message_id || m.messageId || `msg-${Math.random().toString(36).slice(2, 9)}`) as string;
          const role = (m.role || (m.user_id ? 'user' : 'agent')) as string;
          let content = '';
          if (typeof m.content === 'string') {
            content = m.content;
          } else if (partArray) {
            content = extractPartTexts(partArray).join('\n');
          } else if (typeof m.parts === 'string') {
            content = m.parts;
          }
          const ts = m.created_at || m.timestamp || m.sort_key || null;
          const timestamp = ts ? new Date(ts as string) : new Date();
          
          // Reconstruct timeline from saved message data
          const timeline = reconstructTimelineFromMessage(m);
          
          // Check if this is an activity-log-only message (should only appear in timeline, not as MessageCard)
          // Detect via extensions on the nested status message
          const kind = m.kind as string | undefined;
          let rawPayload: Record<string, unknown> | null = null;
          if (typeof m.raw_payload === 'string' && m.raw_payload) {
            try { rawPayload = JSON.parse(m.raw_payload); } catch { /* ignore */ }
          }
          const savedStatusMsg = (rawPayload?.status as any)?.message;
          const savedExtensions = (savedStatusMsg?.extensions || []) as string[];
          const isActivityLogOnly = savedExtensions.includes(ACTIVITY_LOG_EXT);
          
          // Determine if message has actual content worth displaying in MessageCard
          const isStatusOnlyMessage = 
            content.startsWith('Status: ') || 
            content.startsWith('Task submitted:') ||
            content.startsWith('Agent execution') ||
            content.startsWith('Delegating to ') ||
            content.startsWith('Using ') ||
            (!content || content.trim().length === 0);
          
          // Mark whether this message should show a MessageCard
          const showMessageCard = !isActivityLogOnly && !(kind === 'status-update' && isStatusOnlyMessage);
          
          return {
            id,
            conversationId,
            type: role === 'user' ? 'user' : 'agent',
            content,
            timestamp,
            parts: partArray, // Include parts array for file attachments
            timeline: timeline.length > 0 ? timeline : undefined, // Only include if we have events
            showMessageCard, // Control whether to render MessageCard component
          } as Message;
        });

      setMessagesMap((prev) => {
        const newMap = new Map(prev);
        const existing = newMap.get(conversationId) || [];
        const existingIds = new Set(existing.map((x) => x.id));
        const merged = [...existing, ...mapped.filter((m) => !existingIds.has(m.id))];
        merged.sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());
        newMap.set(conversationId, merged);
        return newMap;
      });
    } catch (e) {
      console.error('loadMessages failed', e);
    } finally {
      setIsLoadingMessages(false);
    }
  }, []);

  // Create a new conversation
  const createConversation = useCallback(() => {
    const timestamp = new Date();
    const newConv: Conversation = {
      id: generateUUID(),
      title: 'New Conversation',
      lastMessage: '',
      timestamp,
      lastUpdatedRaw: timestamp.toISOString(),
      status: 'active',
      hasActiveTasks: false,
    };
    setConversations((prev) => [newConv, ...prev]);
    // Pre-seed an empty messages array so loadMessages doesn't fire for this new conversation
    setMessagesMap((prev) => {
      const newMap = new Map(prev);
      newMap.set(newConv.id, []);
      return newMap;
    });
    setActiveConversationId(newConv.id);
  }, []);

  // Interrupt/cancel the current task
  const interruptTask = useCallback(() => {
    if (!activeConversationId) return;
    cancelTask(activeConversationId);
    setWaitingMap((prev) => {
      const m = new Map(prev);
      m.delete(activeConversationId);
      return m;
    });
  }, [activeConversationId, cancelTask]);

  // Select a conversation
  const selectConversation = useCallback(
    (id: string) => {
      setActiveConversationId(id);
      const existingMessages = messagesMap.get(id);
      if (!existingMessages || existingMessages.length === 0) {
        loadMessages(id);
      }
    },
    [messagesMap, loadMessages]
  );

  // Send a message
  const sendMessageAction = useCallback(
    (
      content: string, 
      files?: Array<Pick<UploadedFileInfo, 'uri' | 'mimeType' | 'name' | 's3Url'>>
    ) => {
      // Allow messages with either text content or file attachments
      const hasContent = content.trim().length > 0;
      const hasFiles = files && files.length > 0;
      
      if (!isConnected || (!hasContent && !hasFiles)) return;

      let conversationId = activeConversationId;
      const isFirstMessage = conversationId ? (messagesMap.get(conversationId)?.length || 0) === 0 : true;

      // Create new conversation if needed
      if (!conversationId) {
        conversationId = generateUUID();
        // Use content for title if available, otherwise use file attachment info
        const displayText = hasContent 
          ? content 
          : hasFiles 
          ? `Sent ${files!.length} file${files!.length > 1 ? 's' : ''}`
          : 'New conversation';
        const title = displayText.slice(0, 40) + (displayText.length > 40 ? '...' : '');
        const timestamp = new Date();
        const newConv: Conversation = {
          id: conversationId,
          title,
          lastMessage: displayText.slice(0, 50),
          timestamp,
          lastUpdatedRaw: timestamp.toISOString(),
          status: 'active',
          hasActiveTasks: false,
        };
        setConversations((prev) => [newConv, ...prev]);
        setActiveConversationId(conversationId);
      }

      // Add user message
      const messageId = generateUUID();
      pendingMessagesRef.current.set(messageId, conversationId);
      messageCounterRef.current++;
      
      // Build parts array for message (text + files)
      const parts: Array<{ kind: string; text?: string; file?: { uri: string; mimeType?: string; name?: string } }> = [];
      
      // Add text part only if there's content
      if (hasContent) {
        parts.push({ kind: 'text', text: content });
      }
      
      if (hasFiles) {
        files!.forEach((file) => {
          parts.push({
            kind: 'file',
            file: {
              uri: file.uri,
              mimeType: file.mimeType,
              name: file.name,
            },
          });
        });
      }
      
      // Use content for display if available, otherwise describe the attachments
      const displayContent = hasContent 
        ? content
        : hasFiles
        ? `Sent ${files!.length} file${files!.length > 1 ? 's' : ''}: ${files!.map(f => f.name).join(', ')}`
        : '';
      
      const userMsg: Message = {
        id: messageId,
        conversationId,
        type: 'user',
        content: displayContent,
        timestamp: new Date(),
        parts,
      };
      addMessage(conversationId, userMsg);

      // Update conversation with appropriate last message text
      const lastMessageText = hasContent 
        ? content.slice(0, 50)
        : hasFiles
        ? `📎 ${files!.length} file${files!.length > 1 ? 's' : ''}`
        : 'New message';
      
      updateConversation(conversationId, {
        lastMessage: lastMessageText,
        timestamp: new Date(),
      });

      if (isFirstMessage) {
        const displayText = hasContent 
          ? content 
          : hasFiles 
          ? `Sent ${files!.length} file${files!.length > 1 ? 's' : ''}`
          : 'New conversation';
        const title = displayText.slice(0, 40) + (displayText.length > 40 ? '...' : '');
        updateConversation(conversationId, { title });
      }

      // Build metadata - prefer local settings, fallback to user preferences from database
      const metadata: Record<string, any> = {};
      const effectiveModel = settings?.model || userSettings?.preferred_model;
      const effectiveEnableThinking = settings?.enableThinking ?? userSettings?.enable_thinking;
      const effectiveThinkingLevel = settings?.thinkingLevel || userSettings?.thinking_level;
      
      if (effectiveModel) {
        metadata.model = effectiveModel;
      }
      if (effectiveEnableThinking !== undefined) {
        metadata.enableThinking = String(effectiveEnableThinking);
      }
      if (effectiveThinkingLevel) {
        metadata.thinkingLevel = effectiveThinkingLevel;
      }

      // Get context ID if available
      const contextId = contextIdsMap.get(conversationId);

      // Build file attachments array
      let fileAttachments: Array<{ uri: string; mimeType: string; name: string; s3Url?: string }> | undefined;
      if (hasFiles) {
        fileAttachments = files!.map(f => ({
          uri: f.uri,
          mimeType: f.mimeType,
          name: f.name,
          s3Url: f.s3Url, // Include s3Url for backend processing
        }));
      }

      // Send simple payload - backend converts to A2A format
      const payload: any = {
        id: messageId,
        conversationId,
        message: content,
        sessionId,
        metadata,
        ...(contextId && { contextId }),
      };

      if (fileAttachments && fileAttachments.length > 0) {
        payload.fileAttachments = fileAttachments;
      }

      // Send via socket
      setWaitingMap((prev) => new Map(prev).set(conversationId, true));
      socketSendMessage(payload);
    },
    [
      activeConversationId,
      isConnected,
      messagesMap,
      settings,
      userSettings,
      contextIdsMap,
      sessionId,
      addMessage,
      updateConversation,
      socketSendMessage,
    ]
  );

  // Update settings and initialize connection
  const updateSettings = useCallback(
    async (newSettings: Settings): Promise<boolean> => {
      setSettings(newSettings);
      const success = await initializeClient(newSettings, sessionId);
      if (success) {
        // Reload conversations with new agent URL
        loadConversations();
      }
      return success;
    },
    [initializeClient, sessionId, setSettings, loadConversations]
  );

  // Load messages when activeConversationId changes
  // This ensures messages are loaded when returning to a conversation after navigation
  // Skip if messagesMap already has an entry (including empty [] from createConversation)
  useEffect(() => {
    if (!activeConversationId) return;
    
    if (!messagesMap.has(activeConversationId)) {
      loadMessages(activeConversationId);
    }
  }, [activeConversationId, messagesMap, loadMessages]);

  // Default settings for auto-initialization
  const DEFAULT_SETTINGS: Settings = {
    agentUrl: config.orchestratorUrl,
    model: 'claude-sonnet-4.5',
  };

  // Initialize on mount - use existing settings or defaults
  // We need to wait for both sessionId and socket to be ready
  useEffect(() => {
    // Skip if no sessionId yet (it may be generated async)
    if (!sessionId) return;
    
    // Skip if socket is not ready yet
    if (!isSocketReady) return;
    
    // Skip if already connected to agent
    if (isConnected) return;

    const effectiveSettings = settings || DEFAULT_SETTINGS;
    
    // If no settings exist, set the defaults (ephemeral, not persisted)
    if (!settings) {
      setSettings(DEFAULT_SETTINGS);
    }
    
    console.log('Auto-initializing chat with settings (ephemeral):', effectiveSettings);
    initializeClient(effectiveSettings, sessionId).then((success) => {
      console.log('Auto-initialization result:', success);
      if (success) {
        loadConversations();
      }
    });
  }, [sessionId, isSocketReady, isConnected]); // Re-run when dependencies change

  return (
    <ChatContext.Provider
      value={{
        conversations,
        activeConversationId,
        messages,
        tasks,
        settings,
        userSettings: userSettings || null,
        isLoadingConversations,
        isLoadingMessages,
        isConnected,
        isWaiting,
        streamingMessage,
        liveWorkingSteps,
        liveSubagentThoughts,
        liveStatusHistory,
        liveTimeline,
        createConversation,
        interruptTask,
        selectConversation,
        sendMessage: sendMessageAction,
        updateSettings,
        loadConversations,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChat(): ChatContextType {
  const context = useContext(ChatContext);
  if (context === undefined) {
    throw new Error('useChat must be used within a ChatProvider');
  }
  return context;
}
