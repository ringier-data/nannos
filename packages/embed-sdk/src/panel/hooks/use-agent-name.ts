/**
 * The name to show for the agent, for hosts that render their own chrome
 * (ADR-0006). Precedence: the adapter's explicit `agentName` → the name the
 * host itself publishes at `/.well-known/agent-skills/index.json` → the A2A
 * handshake's agent name → null while nothing is known yet.
 */
import { useEffect, useState, useSyncExternalStore } from 'react';
import { useChatEngine } from '../engine';

export function useAgentName(): string | null {
  const { adapter, core, connection } = useChatEngine();
  const [hostName, setHostName] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    void core.resolveHostAgentName().then((name) => {
      if (alive) setHostName(name);
    });
    return () => {
      alive = false;
    };
  }, [core]);
  const handshakeName = useSyncExternalStore(connection.subscribe, connection.getSnapshot).agentName;
  return adapter.agentName ?? hostName ?? handshakeName ?? null;
}
