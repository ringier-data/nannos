/**
 * Escape hatch for host channels outside the chat protocol (e.g. console's
 * `catalog_sync_progress`): subscribe to any server-sent socket event on the
 * scope's connection. The callback rides a ref, so an inline closure is fine.
 */
import { useEffect, useRef } from 'react';
import { useAssistant } from '../../react';
import { useChatEngineOptional } from '../engine';

export function useSocketEvent(event: string, callback: (data: unknown) => void): void {
  // Inside a chat scope, its (possibly playground-scoped) socket; anywhere else
  // under <NannosProvider>, the provider core's socket.
  const engine = useChatEngineOptional();
  const core = useAssistant().core;
  const client = engine?.client ?? core?.transport ?? null;
  const callbackRef = useRef(callback);
  callbackRef.current = callback;

  useEffect(() => {
    if (!client) return;
    return client.onEvent(event, (data) => callbackRef.current(data));
  }, [client, event]);
}
