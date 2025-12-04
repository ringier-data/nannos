import type { ReactNode } from 'react';
import { SocketProvider, ChatProvider } from './contexts';
import { ChatApp } from './ChatApp';

interface ChatAppWrapperProps {
  socketPath?: string;
}

/**
 * Main entry point for the Chat application.
 * Wraps the ChatApp with all necessary providers.
 */
export function ChatAppWrapper({ socketPath }: ChatAppWrapperProps) {
  return (
    <SocketProvider socketPath={socketPath}>
      <ChatProvider>
        <ChatApp />
      </ChatProvider>
    </SocketProvider>
  );
}

/**
 * Provider component for embedding chat in other layouts.
 * Use this if you want to access chat context from outside components.
 */
export function ChatProviders({
  children,
  socketPath,
}: {
  children: ReactNode;
  socketPath?: string;
}) {
  return (
    <SocketProvider socketPath={socketPath}>
      <ChatProvider>{children}</ChatProvider>
    </SocketProvider>
  );
}

// Re-export everything for convenient imports
export { ChatApp } from './ChatApp';
export { SocketProvider, ChatProvider, useSocket, useChat } from './contexts';
export * from './components';
export * from './types';
export * from './utils';
export * from './hooks/useLocalStorage';
