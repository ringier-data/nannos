import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { io, type Socket } from 'socket.io-client';
import type { AgentInfo, AgentResponseData, ClientInitializedData, SendMessagePayload, Settings } from '../types';

interface SocketContextType {
  socket: Socket | null;
  isConnected: boolean;
  isSocketReady: boolean;
  agentInfo: AgentInfo | null;
  initializeClient: (settings: Settings, sessionId: string) => Promise<boolean>;
  sendMessage: (payload: SendMessagePayload) => void;
  onAgentResponse: (callback: (data: AgentResponseData) => void) => () => void;
}

const SocketContext = createContext<SocketContextType | undefined>(undefined);

interface SocketProviderProps {
  children: ReactNode;
  socketPath?: string;
  customHeaders?: Record<string, string>;
}

export function SocketProvider({ children, socketPath = '/api/v1/socket.io', customHeaders = {} }: SocketProviderProps) {
  const [socket, setSocket] = useState<Socket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isSocketReady, setIsSocketReady] = useState(false);
  const [agentInfo, setAgentInfo] = useState<AgentInfo | null>(null);
  const responseCallbacksRef = useRef<Set<(data: AgentResponseData) => void>>(new Set());
  const initResolveRef = useRef<((value: boolean) => void) | null>(null);
  const customHeadersRef = useRef(customHeaders);

  // Initialize socket connection
  useEffect(() => {
    const newSocket = io({ path: socketPath });

    newSocket.on('connect', () => {
      console.log('Socket connected');
      setIsSocketReady(true);
    });

    newSocket.on('disconnect', () => {
      console.log('Socket disconnected');
      setIsConnected(false);
      setIsSocketReady(false);
      setAgentInfo(null);
    });

    newSocket.on('client_initialized', (data: ClientInitializedData) => {
      if (data.status === 'success') {
        setIsConnected(true);
        setAgentInfo(data.agent || null);
        initResolveRef.current?.(true);
      } else {
        setIsConnected(false);
        setAgentInfo(null);
        console.error('Client initialization failed:', data.error || data.message);
        initResolveRef.current?.(false);
      }
      initResolveRef.current = null;
    });

    newSocket.on('agent_response', (data: AgentResponseData) => {
      responseCallbacksRef.current.forEach((callback) => callback(data));
    });

    newSocket.on('debug_log', (data: unknown) => {
      console.log('Debug log:', data);
    });

    setSocket(newSocket);

    return () => {
      newSocket.disconnect();
    };
  }, [socketPath]);

  const initializeClient = useCallback(
    (settings: Settings, sessionId: string): Promise<boolean> => {
      return new Promise((resolve) => {
        if (!socket) {
          console.error('Socket not initialized');
          resolve(false);
          return;
        }

        const timeout = setTimeout(() => {
          console.error('Initialize client timeout');
          setIsConnected(false);
          initResolveRef.current = null;
          resolve(false);
        }, 15000);

        initResolveRef.current = (success: boolean) => {
          clearTimeout(timeout);
          resolve(success);
        };

        socket.emit('initialize_client', {
          url: settings.agentUrl,
          customHeaders: customHeadersRef.current,
          sessionId,
        });
      });
    },
    [socket]
  );

  const sendMessage = useCallback(
    (payload: SendMessagePayload) => {
      if (!socket?.connected) {
        console.error('Socket not connected');
        return;
      }
      socket.emit('send_message', payload);
    },
    [socket]
  );

  const onAgentResponse = useCallback((callback: (data: AgentResponseData) => void) => {
    responseCallbacksRef.current.add(callback);
    return () => {
      responseCallbacksRef.current.delete(callback);
    };
  }, []);

  return (
    <SocketContext.Provider
      value={{
        socket,
        isConnected,
        isSocketReady,
        agentInfo,
        initializeClient,
        sendMessage,
        onAgentResponse,
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
