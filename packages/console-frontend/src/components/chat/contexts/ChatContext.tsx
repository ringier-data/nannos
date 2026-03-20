import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { AgentResponseData, Conversation, Message, Settings, Task, TaskHistoryEntry } from '../types';
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
  userSettings: {
    preferred_model?: string | null;
    enable_thinking?: boolean | null;
    thinking_level?: string | null;
  } | null;
  isLoadingConversations: boolean;
  isLoadingMessages: boolean;
  isConnected: boolean;

  // Actions
  createConversation: () => void;
  selectConversation: (id: string) => void;
  sendMessage: (content: string, files?: Array<Pick<UploadedFileInfo, 'uri' | 'mimeType' | 'name' | 's3Url'>>) => void;
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

export function ChatProvider({ children, playgroundMode }: ChatProviderProps) {
  const sessionId = useSessionId();
  const { isConnected, isSocketReady, initializeClient, sendMessage: socketSendMessage, onAgentResponse } = useSocket();

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
  const [isLoadingConversations, setIsLoadingConversations] = useState(false);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);
  // Settings are ephemeral and not persisted to localStorage
  const [settings, setSettings] = useState<Settings | null>(null);

  const messageCounterRef = useRef(0);
  const taskCounterRef = useRef(0);
  const playgroundModeRef = useRef(playgroundMode);

  // Derived state
  const messages = activeConversationId ? messagesMap.get(activeConversationId) || [] : [];
  const tasks = activeConversationId ? tasksMap.get(activeConversationId) || [] : [];

  // Helper to add a message
  const addMessage = useCallback((conversationId: string, message: Message) => {
    setMessagesMap((prev) => {
      const newMap = new Map(prev);
      const existing = newMap.get(conversationId) || [];
      newMap.set(conversationId, [...existing, message]);
      return newMap;
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
      if (!activeConversationId) {
        console.warn('Received response but no active conversation');
        return;
      }

      // Save context_id
      if (data.contextId) {
        setContextIdsMap((prev) => {
          const newMap = new Map(prev);
          newMap.set(activeConversationId, data.contextId!);
          return newMap;
        });
      }

      // Handle error
      if (data.error) {
        messageCounterRef.current++;
        const errorMsg: Message = {
          id: `msg-${messageCounterRef.current}`,
          conversationId: activeConversationId,
          type: 'agent',
          content: `Error: ${data.error}`,
          timestamp: new Date(),
        };
        addMessage(activeConversationId, errorMsg);
        updateConversation(activeConversationId, { lastMessage: errorMsg.content });
        return;
      }

      // Handle message response
      if (data.role === 'agent' && Array.isArray(data.parts)) {
        if (shouldDisplayMessageParts(data.parts)) {
          const text = extractPartTexts(data.parts).join('\n');
          messageCounterRef.current++;
          const agentMsg: Message = {
            id: `msg-${messageCounterRef.current}`,
            conversationId: activeConversationId,
            type: 'agent',
            content: text,
            timestamp: new Date(),
          };
          addMessage(activeConversationId, agentMsg);
          updateConversation(activeConversationId, {
            lastMessage: text.slice(0, 50),
            timestamp: new Date(),
          });
        }
        return;
      }

      // Handle task status update
      if (data.status) {
        // Extract nested message if present
        const nestedMsg = data.status.message;
        if (nestedMsg && Array.isArray(nestedMsg.parts) && shouldDisplayMessageParts(nestedMsg.parts)) {
          const text = extractPartTexts(nestedMsg.parts).join('\n');
          messageCounterRef.current++;
          const agentMsg: Message = {
            id: `msg-${messageCounterRef.current}`,
            conversationId: activeConversationId,
            type: 'agent',
            content: text,
            timestamp: new Date(),
          };
          addMessage(activeConversationId, agentMsg);
          updateConversation(activeConversationId, {
            lastMessage: text.slice(0, 50),
            timestamp: new Date(),
          });
        }

        // Update task
        const taskId = data.id || `task-${taskCounterRef.current++}`;
        const existingTasks = tasksMap.get(activeConversationId) || [];
        const existingTask = existingTasks.find((t) => t.id === taskId);

        const normalizedStatus = getTaskState(data.status?.state);
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
              conversationId: activeConversationId,
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

        addOrUpdateTask(activeConversationId, task);

        // Update conversation active tasks status
        const allTasks = tasksMap.get(activeConversationId) || [];
        const hasActiveTasks = allTasks.some((t) => !isTaskComplete(t.status));
        updateConversation(activeConversationId, { hasActiveTasks });
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
            conversationId: activeConversationId,
            type: 'agent',
            content: text,
            timestamp: new Date(),
          };
          addMessage(activeConversationId, agentMsg);
          updateConversation(activeConversationId, {
            lastMessage: text.slice(0, 50),
            timestamp: new Date(),
          });
        }

        // Update task with artifact
        const taskId = data.id || `task-${taskCounterRef.current++}`;
        const existingTasks = tasksMap.get(activeConversationId) || [];
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
              conversationId: activeConversationId,
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

        addOrUpdateTask(activeConversationId, task);
        return;
      }

      // Unknown response - display as JSON
      messageCounterRef.current++;
      const genericMsg: Message = {
        id: `msg-${messageCounterRef.current}`,
        conversationId: activeConversationId,
        type: 'agent',
        content: JSON.stringify(data, null, 2),
        timestamp: new Date(),
      };
      addMessage(activeConversationId, genericMsg);
    });

    return unsubscribe;
  }, [activeConversationId, onAgentResponse, addMessage, addOrUpdateTask, updateConversation, tasksMap]);

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
        throw new Error(`Failed to load messages (status=${resp.status})`);
      }
      const data = await resp.json();

      const raw = Array.isArray(data.items) ? data.items : Array.isArray(data.messages) ? data.messages : [];

      const mapped: Message[] = raw
        .map((m: Record<string, unknown>) => {
          const partArray = m.parts as
            | Array<{ kind?: string; text?: string; file?: { uri: string; mimeType?: string; name?: string } }>
            | undefined;
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
          return {
            id,
            conversationId,
            type: role === 'user' ? 'user' : 'agent',
            content,
            timestamp,
            parts: partArray, // Include parts array for file attachments
          } as Message;
        })
        .filter(Boolean) as Message[];

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
    setActiveConversationId(newConv.id);
  }, []);

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
    (content: string, files?: Array<Pick<UploadedFileInfo, 'uri' | 'mimeType' | 'name' | 's3Url'>>) => {
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
      messageCounterRef.current++;

      // Build parts array for message (text + files)
      const parts: Array<{ kind: string; text?: string; file?: { uri: string; mimeType?: string; name?: string } }> =
        [];

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
          ? `Sent ${files!.length} file${files!.length > 1 ? 's' : ''}: ${files!.map((f) => f.name).join(', ')}`
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
        fileAttachments = files!.map((f) => ({
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

  // Default settings for auto-initialization — no model preset so the
  // orchestrator uses its own default (which respects OPENAI_COMPATIBLE_MODEL).
  const DEFAULT_SETTINGS: Settings = {
    agentUrl: config.orchestratorUrl,
    model: '',
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
        createConversation,
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
