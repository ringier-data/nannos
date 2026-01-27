import { useCallback, useEffect, useRef, useState } from 'react';
import { io, Socket } from 'socket.io-client';
import { v7 as uuidv7 } from 'uuid';
import type { AgentResponseData, SendMessagePayload } from '@/components/chat/types';
import { config } from '@/config';
import {
  getAdminModeFromStorage,
  getImpersonatedUserIdFromStorage,
  ADMIN_MODE_HEADER,
  IMPERSONATE_USER_HEADER,
} from '@/api/apiInstanceConfig';

export interface PlaygroundMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
}

export interface PlaygroundConversation {
  id: string;
  title: string;
  messages: PlaygroundMessage[];
  configVersion: number;
  createdAt: Date;
  updatedAt: Date;
}

interface UsePlaygroundChatOptions {
  /** The version hash of the sub-agent config to test */
  subAgentConfigHash: string;
  subAgentName: string;
  /** The version number for display purposes */
  configVersion: number;
  agentUrl?: string;
}

interface UsePlaygroundChatReturn {
  // State
  conversations: PlaygroundConversation[];
  activeConversationId: string | null;
  isConnected: boolean;
  isLoading: boolean;
  isLoadingConversations: boolean;
  currentMessages: PlaygroundMessage[];

  // Actions
  createConversation: (configVersion: number) => PlaygroundConversation;
  selectConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
  sendMessage: (content: string, configVersion: number) => Promise<void>;
  loadConversations: () => Promise<void>;
}

function generateConversationTitle(messages: PlaygroundMessage[]): string {
  if (messages.length === 0) return 'New Conversation';
  const firstUserMessage = messages.find((m) => m.role === 'user');
  if (!firstUserMessage) return 'New Conversation';
  const title = firstUserMessage.content.slice(0, 40);
  return title.length < firstUserMessage.content.length ? `${title}...` : title;
}

function generateUUID(): string {
  return uuidv7();
}

function extractPartTexts(parts: Array<{ text?: string; kind?: string }>): string[] {
  return parts
    .filter((p) => p.text)
    .map((p) => p.text as string);
}

function shouldDisplayMessageParts(parts: Array<{ text?: string; kind?: string }>): boolean {
  return parts.some((p) => p.text && p.text.trim().length > 0);
}

export function usePlaygroundChat({
  subAgentConfigHash,
  subAgentName,
  configVersion,
  agentUrl = config.orchestratorUrl,
}: UsePlaygroundChatOptions): UsePlaygroundChatReturn {
  const [socket, setSocket] = useState<Socket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingConversations, setIsLoadingConversations] = useState(false);
  const [conversations, setConversations] = useState<PlaygroundConversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  
  const sessionIdRef = useRef<string>(generateUUID());
  const pendingMessageRef = useRef<{ conversationId: string; resolve: () => void } | null>(null);
  const contextIdMapRef = useRef<Map<string, string>>(new Map());
  const lastLoadedHashRef = useRef<string>('');
  const timeoutIdRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loadedConversationsRef = useRef<Set<string>>(new Set());

  // Get current conversation's messages
  const currentMessages = activeConversationId
    ? conversations.find((c) => c.id === activeConversationId)?.messages || []
    : [];

  // Initialize socket connection - only when we have a valid config hash
  useEffect(() => {
    // Don't connect without a valid config hash
    if (!subAgentConfigHash) {
      console.log('[Playground] Waiting for config hash before connecting');
      return;
    }

    const newSocket = io({ path: '/api/v1/socket.io' });

    newSocket.on('connect', () => {
      console.log('[Playground] Socket connected');
      
      // Initialize with playground headers using config hash
      newSocket.emit('initialize_client', {
        url: agentUrl,
        customHeaders: {
          'X-Playground-SubAgentConfig-Hash': subAgentConfigHash,
        },
        sessionId: sessionIdRef.current,
      });
    });

    newSocket.on('client_initialized', (data: { status: string; error?: string }) => {
      if (data.status === 'success') {
        console.log('[Playground] Client initialized successfully');
        setIsConnected(true);
      } else {
        console.error('[Playground] Client initialization failed:', data.error);
        setIsConnected(false);
      }
    });

    newSocket.on('disconnect', () => {
      console.log('[Playground] Socket disconnected');
      setIsConnected(false);
    });

    newSocket.on('agent_response', (data: AgentResponseData) => {
      handleAgentResponse(data);
    });

    setSocket(newSocket);

    return () => {
      newSocket.disconnect();
    };
  }, [subAgentConfigHash, agentUrl]);

  // Load conversations when connected or when version changes
  useEffect(() => {
    if (isConnected && subAgentConfigHash) {
      loadConversations();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isConnected, subAgentConfigHash, configVersion]);

  // Reset timeout on each response
  const resetTimeout = useCallback((conversationId: string) => {
    // Clear existing timeout
    if (timeoutIdRef.current) {
      clearTimeout(timeoutIdRef.current);
      timeoutIdRef.current = null;
    }
    
    // Set new 5-minute timeout
    timeoutIdRef.current = setTimeout(() => {
      if (pendingMessageRef.current?.conversationId === conversationId) {
        console.warn('[Playground] Request timed out after 5 minutes');
        setIsLoading(false);
        pendingMessageRef.current?.resolve();
        pendingMessageRef.current = null;
        timeoutIdRef.current = null;
      }
    }, 300000); // 5 minutes = 300,000ms
  }, []);

  // Helper to add message to conversation
  const addMessageToConversation = useCallback((conversationId: string, message: PlaygroundMessage) => {
    setConversations((prev) =>
      prev.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              // Prevent duplicate messages by checking if message ID already exists
              messages: c.messages.some(m => m.id === message.id) 
                ? c.messages 
                : [...c.messages, message],
              title: c.messages.length === 0 && message.role === 'user' 
                ? generateConversationTitle([message]) 
                : c.title,
              updatedAt: new Date(),
            }
          : c
      )
    );
  }, []);

  // Handle agent responses
  const handleAgentResponse = useCallback((data: AgentResponseData) => {
    const conversationId = pendingMessageRef.current?.conversationId;
    if (!conversationId) {
      console.warn('[Playground] Received response but no pending conversation - this may be a late-arriving response after conversation was already completed');
      return;
    }
    
    // Reset timeout on every response
    resetTimeout(conversationId);

    console.log('[Playground] Handling agent response:', { 
      role: data.role, 
      hasStatus: !!data.status,
      statusState: data.status?.state,
      hasStatusMessage: !!(data.status?.message),
      hasStatusMessageParts: Array.isArray(data.status?.message?.parts),
      hasError: !!data.error,
      hasArtifact: !!(data.artifact || data.kind === 'artifact-update'),
      hasParts: Array.isArray(data.parts),
      fullData: data  // Log full data for debugging
    });

    // Save context_id
    if (data.contextId) {
      contextIdMapRef.current.set(conversationId, data.contextId);
    }

    // Handle error - always clears loading
    if (data.error) {
      const errorMessage: PlaygroundMessage = {
        id: generateUUID(),
        role: 'assistant',
        content: `Error: ${data.error}`,
        timestamp: new Date(),
      };
      addMessageToConversation(conversationId, errorMessage);
      console.log('[Playground] Added error message, clearing loading state');
      // Clear timeout
      if (timeoutIdRef.current) {
        clearTimeout(timeoutIdRef.current);
        timeoutIdRef.current = null;
      }
      setIsLoading(false);
      pendingMessageRef.current?.resolve();
      pendingMessageRef.current = null;
      return;
    }

    // Handle task status update FIRST (before checking role='agent')
    // This ensures we process status updates even if they have role='agent'
    if (data.status) {
      const nestedMsg = data.status.message;
      const state = data.status.state?.toLowerCase();
      
      // Determine state category first
      const isTerminalState = state === 'completed' || state === 'failed' || state === 'cancelled' || state === 'input-required';
      const isProgressState = state === 'working' || state === 'running' || state === 'in_progress' || state === 'pending' || state === 'submitted';
      
      // Add message if present and displayable
      let hasMessage = false;
      if (nestedMsg && Array.isArray(nestedMsg.parts) && shouldDisplayMessageParts(nestedMsg.parts)) {
        const text = extractPartTexts(nestedMsg.parts).join('\n');
        const agentMessage: PlaygroundMessage = {
          id: generateUUID(),
          role: 'assistant',
          content: text,
          timestamp: new Date(),
        };
        addMessageToConversation(conversationId, agentMessage);
        hasMessage = true;
        console.log('[Playground] Added agent message from status.message');
      }
      
      // Clear loading and pendingRef only for terminal states
      if (isTerminalState) {
        console.log('[Playground] Terminal state, clearing loading', { state });
        // Clear timeout
        if (timeoutIdRef.current) {
          clearTimeout(timeoutIdRef.current);
          timeoutIdRef.current = null;
        }
        setIsLoading(false);
        pendingMessageRef.current?.resolve();
        pendingMessageRef.current = null;
        return;
      }
      
      // For progress states (working/running/etc): display message but keep spinner running
      if (isProgressState) {
        console.log('[Playground] Progress state, keeping spinner', { state, hasMessage });
        // Message already added above if present, spinner stays active, pendingRef stays set
        return;
      }
      
      // Unknown state - don't clear loading yet, let timeout handle it
      console.log('[Playground] Unknown state, keeping spinner', { state, hasMessage });
      return;
    }

    // Handle direct agent message response (role='agent' with parts)
    // ONLY if it doesn't have a status (status is handled above)
    if (data.role === 'agent' && Array.isArray(data.parts) && !data.status) {
      if (shouldDisplayMessageParts(data.parts)) {
        const text = extractPartTexts(data.parts).join('\n');
        const agentMessage: PlaygroundMessage = {
          id: generateUUID(),
          role: 'assistant',
          content: text,
          timestamp: new Date(),
        };
        addMessageToConversation(conversationId, agentMessage);
        console.log('[Playground] Added agent message from parts (no status), clearing loading state');
      }
      // Clear loading when we get a direct agent message without status
      // Clear timeout
      if (timeoutIdRef.current) {
        clearTimeout(timeoutIdRef.current);
        timeoutIdRef.current = null;
      }
      setIsLoading(false);
      pendingMessageRef.current?.resolve();
      pendingMessageRef.current = null;
      return;
    }

    // Handle artifact update - don't automatically clear loading
    // Let terminal status or timeout handle completion
    if (data.artifact || data.kind === 'artifact-update') {
      const art = data.artifact || 
        (Array.isArray(data.artifacts) ? (data.artifacts as { parts?: { text?: string }[] }[])[0] : null);
      if (art && Array.isArray(art.parts) && shouldDisplayMessageParts(art.parts)) {
        const text = extractPartTexts(art.parts).join('\n');
        const agentMessage: PlaygroundMessage = {
          id: generateUUID(),
          role: 'assistant',
          content: text,
          timestamp: new Date(),
        };
        addMessageToConversation(conversationId, agentMessage);
        console.log('[Playground] Added agent message from artifact, keeping spinner for final status');
      }
      // Don't clear loading - wait for terminal status or timeout
      return;
    }

    // Unknown response type - log but keep spinner for safety (will timeout after 5 minutes)
    console.log('[Playground] Response did not match any handler pattern, keeping spinner');
  }, [resetTimeout, addMessageToConversation]);

  // Load conversations from the backend API
  const loadConversations = useCallback(async () => {
    if (!subAgentConfigHash) {
      console.log('[Playground] No config hash, skipping conversation load');
      return;
    }

    // Skip if already loaded for this hash
    if (lastLoadedHashRef.current === subAgentConfigHash) {
      console.log('[Playground] Conversations already loaded for hash:', subAgentConfigHash);
      return;
    }

    setIsLoadingConversations(true);
    try {
      const url = new URL('/api/v1/conversations/', window.location.origin);
      url.searchParams.set('limit', '50');
      url.searchParams.set('sub_agent_config_hash', subAgentConfigHash);

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

      console.log('[Playground] Loading conversations for hash:', subAgentConfigHash);
      const resp = await fetch(url.toString(), { credentials: 'include', headers });
      
      if (!resp.ok) {
        throw new Error(`Failed to load conversations (status=${resp.status})`);
      }
      
      const data = await resp.json();
      const raw = Array.isArray(data.items) 
        ? data.items 
        : Array.isArray(data.conversations) 
          ? data.conversations 
          : [];

      console.log('[Playground] Loaded', raw.length, 'conversations from backend');

      // Map backend conversations to PlaygroundConversation
      const mapped: PlaygroundConversation[] = raw.map((c: Record<string, unknown>) => {
        const id = (c.id || c.conversation_id || c.conversationId) as string;
        const title = (c.title || 'Conversation') as string;
        const ts = c.last_message_at || c.lastMessageAt || c.updated_at || c.created_at || null;
        const contextId = c.context_id as string | undefined;

        // Cache context_id for sending messages
        if (contextId) {
          contextIdMapRef.current.set(id, contextId);
        }

        return {
          id,
          title,
          messages: [], // Will be loaded when conversation is selected
          configVersion, // Use the current version being viewed
          createdAt: ts ? new Date(ts as string) : new Date(),
          updatedAt: ts ? new Date(ts as string) : new Date(),
        };
      });

      setConversations(mapped);

      // Mark this hash as loaded
      lastLoadedHashRef.current = subAgentConfigHash;
      
      // Clear loaded conversations tracking for new hash
      loadedConversationsRef.current.clear();

      // Auto-select the most recent conversation (first in list) only if no conversation is currently active
      if (mapped.length > 0) {
        // Capture current activeConversationId from state
        setActiveConversationId((currentActiveId) => {
          // Only auto-select if there's no active conversation or if active conversation no longer exists
          if (!currentActiveId || !mapped.some(c => c.id === currentActiveId)) {
            return mapped[0].id;
          }
          // Keep existing active conversation
          return currentActiveId;
        });
        // Load messages for the selected conversation will happen via effect
      } else {
        setActiveConversationId(null);
      }
    } catch (e) {
      console.error('[Playground] loadConversations failed', e);
    } finally {
      setIsLoadingConversations(false);
    }
  }, [subAgentConfigHash, configVersion]);

  // Load messages for a specific conversation
  const loadMessagesForConversation = useCallback(async (conversationId: string) => {
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

      console.log('[Playground] Loading messages for conversation:', conversationId);
      const resp = await fetch(url.toString(), { credentials: 'include', headers });
      
      if (!resp.ok) {
        throw new Error(`Failed to load messages (status=${resp.status})`);
      }
      
      const data = await resp.json();
      const raw = Array.isArray(data.items) 
        ? data.items 
        : Array.isArray(data.messages) 
          ? data.messages 
          : [];

      console.log('[Playground] Loaded', raw.length, 'messages');

      // Map backend messages to PlaygroundMessage
      const mapped: PlaygroundMessage[] = raw
        .map((m: Record<string, unknown>) => {
          const partArray = m.parts as Array<{ text?: string }> | undefined;
          if (partArray && !shouldDisplayMessageParts(partArray) && typeof m.content !== 'string') {
            return null;
          }

          // Generate a stable fallback ID based on message properties
          const fallbackId = `msg-${m.created_at || m.timestamp || ''}-${m.role || 'unknown'}-${uuidv7()}`;
          const id = (m.id || m.message_id || fallbackId) as string;
          const role = (m.role || (m.user_id ? 'user' : 'agent')) as 'user' | 'assistant';
          
          let content = '';
          if (typeof m.content === 'string') {
            content = m.content;
          } else if (partArray) {
            content = extractPartTexts(partArray).join('\n');
          } else if (typeof m.parts === 'string') {
            content = m.parts;
          }
          
          const ts = m.created_at || m.timestamp || m.sort_key || null;
          
          return {
            id,
            role: role === 'user' ? 'user' : 'assistant',
            content,
            timestamp: ts ? new Date(ts as string) : new Date(),
          } as PlaygroundMessage;
        })
        .filter(Boolean) as PlaygroundMessage[];

      // Sort messages chronologically
      mapped.sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());

      // Deduplicate messages by ID (in case backend returns duplicates)
      const uniqueMessages = mapped.reduce((acc, msg) => {
        if (!acc.some(m => m.id === msg.id)) {
          acc.push(msg);
        }
        return acc;
      }, [] as PlaygroundMessage[]);

      // Update the conversation with loaded messages
      setConversations((prev) =>
        prev.map((c) =>
          c.id === conversationId
            ? { ...c, messages: uniqueMessages }
            : c
        )
      );
      
      // Mark as loaded to prevent repeated loading attempts
      loadedConversationsRef.current.add(conversationId);
    } catch (e) {
      console.error('[Playground] loadMessagesForConversation failed', e);
      // Mark as loaded even on error to prevent infinite retries
      loadedConversationsRef.current.add(conversationId);
    }
  }, []);

  // Auto-load messages when active conversation changes
  useEffect(() => {
    if (activeConversationId && !loadedConversationsRef.current.has(activeConversationId)) {
      loadMessagesForConversation(activeConversationId);
    }
  }, [activeConversationId, loadMessagesForConversation]);

  // Create a new conversation
  const createConversation = useCallback((configVersion: number): PlaygroundConversation => {
    const now = new Date();
    const newConv: PlaygroundConversation = {
      id: generateUUID(),
      title: 'New Conversation',
      messages: [],
      configVersion,
      createdAt: now,
      updatedAt: now,
    };
    setConversations((prev) => [newConv, ...prev]);
    setActiveConversationId(newConv.id);
    // Mark as loaded to prevent auto-loading from backend (which would overwrite local messages)
    loadedConversationsRef.current.add(newConv.id);
    return newConv;
  }, []);

  // Select a conversation
  const selectConversation = useCallback((id: string) => {
    setActiveConversationId(id);
    // Load messages if not already loaded (will be handled by useEffect)
  }, []);

  // Delete a conversation
  const deleteConversation = useCallback((id: string) => {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (activeConversationId === id) {
      const remaining = conversations.filter((c) => c.id !== id);
      setActiveConversationId(remaining.length > 0 ? remaining[0].id : null);
    }
    contextIdMapRef.current.delete(id);
  }, [activeConversationId, conversations]);

  // Send a message
  const sendMessage = useCallback(async (content: string, configVersion: number): Promise<void> => {
    if (!content.trim() || !isConnected || !socket || isLoading) {
      return;
    }

    // Create conversation if none exists
    let conversationId = activeConversationId;
    if (!conversationId) {
      const newConv = createConversation(configVersion);
      conversationId = newConv.id;
    }

    // Add user message
    const userMessage: PlaygroundMessage = {
      id: generateUUID(),
      role: 'user',
      content: content.trim(),
      timestamp: new Date(),
    };
    addMessageToConversation(conversationId, userMessage);

    setIsLoading(true);

    // Send via socket
    return new Promise<void>((resolve) => {
      pendingMessageRef.current = { conversationId: conversationId!, resolve };

      const contextId = contextIdMapRef.current.get(conversationId!);
      const payload: SendMessagePayload = {
        id: generateUUID(),
        conversationId: conversationId!,
        message: content.trim(),
        sessionId: sessionIdRef.current,
        metadata: {
          subAgentConfigHash: subAgentConfigHash,
          playgroundSubagentName: subAgentName,
        },
        ...(contextId && { contextId }),
      };

      socket.emit('send_message', payload);

      // Set initial 5-minute timeout
      resetTimeout(conversationId!);
    });
  }, [
    activeConversationId,
    isConnected,
    socket,
    isLoading,
    createConversation,
    addMessageToConversation,
    subAgentConfigHash,
    subAgentName,
    resetTimeout,
  ]);

  return {
    conversations,
    activeConversationId,
    isConnected,
    isLoading,
    isLoadingConversations,
    currentMessages,
    createConversation,
    selectConversation,
    deleteConversation,
    sendMessage,
    loadConversations,
  };
}
