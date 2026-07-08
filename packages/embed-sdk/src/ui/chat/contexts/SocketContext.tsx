import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { TransportClient } from '../../../core';
import type { ConversationSnapshotData } from '../../../core';
import type { AgentInfo, AgentResponseData, SendMessagePayload, Settings } from '../types';
import { useNannosCoreConfig, useNannosCoreOptional } from '../../adapter';

export type { ConversationSnapshotData };

/**
 * React face of the core TransportClient. Keeps the same hook API the chat
 * components have always used (`isConnected` = handshake done, `isSocketReady`
 * = socket up), but the socket protocol itself lives in the framework-free
 * core. Each SocketProvider instance owns its own TransportClient so scoped
 * providers (e.g. the playground, with its own customHeaders) get their own
 * connection — mirroring the previous per-provider `io()` behavior.
 */
interface SocketContextType {
  isConnected: boolean;
  isSocketReady: boolean;
  agentInfo: AgentInfo | null;
  initializeClient: (settings: Settings, sessionId: string) => Promise<boolean>;
  sendMessage: (payload: SendMessagePayload) => void;
  cancelTask: (conversationId: string) => void;
  onAgentResponse: (callback: (data: AgentResponseData) => void) => () => void;
  subscribeConversation: (conversationId: string) => void;
  unsubscribeConversation: (conversationId: string) => void;
  onConversationSnapshot: (callback: (data: ConversationSnapshotData) => void) => () => void;
  /** Generic escape hatch for host-app socket channels (e.g. catalog progress). */
  onEvent: (event: string, callback: (data: unknown) => void) => () => void;
}

const SocketContext = createContext<SocketContextType | undefined>(undefined);

interface SocketProviderProps {
  children: ReactNode;
  socketPath?: string;
  customHeaders?: Record<string, string>;
}

export function SocketProvider({ children, socketPath, customHeaders = {} }: SocketProviderProps) {
  const baseConfig = useNannosCoreConfig();
  const core = useNannosCoreOptional();
  const [isSocketReady, setIsSocketReady] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [agentInfo, setAgentInfo] = useState<AgentInfo | null>(null);

  const customHeadersKey = JSON.stringify(customHeaders);
  const ownTransport = useMemo(
    () =>
      new TransportClient({
        ...baseConfig,
        ...(socketPath ? { socketPath } : {}),
        customHeaders: { ...baseConfig.customHeaders, ...(JSON.parse(customHeadersKey) as Record<string, string>) },
      }),
    [baseConfig, socketPath, customHeadersKey],
  );

  // Reuse the ONE core transport ONLY when <NannosProvider> owns it (the embed):
  // it's already connected, carries the auth strategy's token, and is reauthed on
  // login() — so chat, status, and launcher-gated login share a single
  // authenticated connection. A bare core (the console via HostAdapterProvider,
  // NO NannosProvider) is NOT provider-managed, so it keeps the original behavior:
  // its own per-provider transport, connected+disconnected here. Scoped
  // customHeaders (console playground) always get a dedicated transport too.
  const useShared =
    !!core && core.transportManagedExternally && Object.keys(customHeaders).length === 0;
  const transport = useShared ? core!.transport : ownTransport;

  useEffect(() => {
    const apply = (s: { socketConnected: boolean; initialized: boolean; agentInfo: AgentInfo | null }) => {
      setIsSocketReady(s.socketConnected);
      setIsConnected(s.initialized);
      setAgentInfo(s.agentInfo);
    };
    const unsubscribe = transport.subscribe(apply);
    // Seed from the CURRENT state: the shared transport is typically already
    // connected by the provider before this widget mounts, so `subscribe` alone
    // (future events only) would miss the connection and hang on "Disconnected".
    apply(transport.getState());
    void transport.connect(); // idempotent — no-op if the provider already connected it
    return () => {
      unsubscribe();
      // Never tear down the shared/provider-owned transport (panel close/reopen
      // must not drop the connection); only dispose one we created here.
      if (!useShared) transport.disconnect();
    };
  }, [transport, useShared]);

  const initializeClient = useCallback(
    (settings: Settings, sessionId: string) => transport.initializeClient(settings, sessionId),
    [transport],
  );
  const sendMessage = useCallback(
    (payload: SendMessagePayload) => {
      if (!transport.sendMessage(payload)) console.error('Socket not connected');
    },
    [transport],
  );
  const cancelTask = useCallback(
    (conversationId: string) => {
      if (!transport.cancelTask(conversationId)) console.error('Socket not connected');
    },
    [transport],
  );
  const onAgentResponse = useCallback(
    (callback: (data: AgentResponseData) => void) => transport.onAgentResponse(callback),
    [transport],
  );
  const onEvent = useCallback(
    (event: string, callback: (data: unknown) => void) => transport.onEvent(event, callback),
    [transport],
  );
  const subscribeConversation = useCallback(
    (conversationId: string) => {
      transport.subscribeConversation(conversationId);
    },
    [transport],
  );
  const unsubscribeConversation = useCallback(
    (conversationId: string) => {
      transport.unsubscribeConversation(conversationId);
    },
    [transport],
  );
  const onConversationSnapshot = useCallback(
    (callback: (data: ConversationSnapshotData) => void) => transport.onConversationSnapshot(callback),
    [transport],
  );

  return (
    <SocketContext.Provider
      value={{
        isConnected,
        isSocketReady,
        agentInfo,
        initializeClient,
        sendMessage,
        cancelTask,
        onAgentResponse,
        subscribeConversation,
        unsubscribeConversation,
        onConversationSnapshot,
        onEvent,
      }}
    >
      {children}
    </SocketContext.Provider>
  );
}

export function useSocket(): SocketContextType {
  const context = useContext(SocketContext);
  if (context === undefined) {
    throw new Error('useSocket must be used within a SocketProvider');
  }
  return context;
}
