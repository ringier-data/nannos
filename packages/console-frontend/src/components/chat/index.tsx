import type { ReactNode } from 'react';
import { SocketProvider, ChatProvider, type PlaygroundMode } from './contexts';
import { ChatApp } from './ChatApp';

interface ChatAppWrapperProps {
  socketPath?: string;
  /** Custom headers to send with socket initialization (e.g., for playground mode) */
  customHeaders?: Record<string, string>;
  /** Playground mode configuration */
  playgroundMode?: PlaygroundMode;
}

interface ChatAppContentProps {
  /** Playground mode configuration */
  playgroundMode?: PlaygroundMode;
}

/**
 * Main entry point for the Chat application.
 * Wraps the ChatApp with all necessary providers.
 * Use when SocketProvider is NOT already in the component tree.
 */
export function ChatAppWrapper({ socketPath, customHeaders, playgroundMode }: ChatAppWrapperProps) {
  return (
    <SocketProvider socketPath={socketPath} customHeaders={customHeaders}>
      <ChatProvider playgroundMode={playgroundMode}>
        <ChatApp />
      </ChatProvider>
    </SocketProvider>
  );
}

/**
 * Chat application content without Socket provider.
 * Use when SocketProvider is already provided by a parent layout (e.g., DashboardLayout).
 */
export function ChatAppContent({ playgroundMode }: ChatAppContentProps) {
  return (
    <ChatProvider playgroundMode={playgroundMode}>
      <ChatApp />
    </ChatProvider>
  );
}

/**
 * Provider component for embedding chat in other layouts.
 * Use this if you want to access chat context from outside components.
 */
export function ChatProviders({
  children,
  socketPath,
  customHeaders,
  playgroundMode,
}: {
  children: ReactNode;
  socketPath?: string;
  customHeaders?: Record<string, string>;
  playgroundMode?: PlaygroundMode;
}) {
  return (
    <SocketProvider socketPath={socketPath} customHeaders={customHeaders}>
      <ChatProvider playgroundMode={playgroundMode}>{children}</ChatProvider>
    </SocketProvider>
  );
}

// Re-export everything for convenient imports
export { ChatApp } from './ChatApp';
export { SocketProvider, ChatProvider, useSocket, useChat } from './contexts';
export type { PlaygroundMode } from './contexts';
export * from './components';
export * from './types';
export * from './utils';
export * from './hooks/useLocalStorage';
