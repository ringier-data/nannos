/**
 * The name to show for the agent, for hosts that render their own chrome
 * (ADR-0006). Precedence: the adapter's explicit `agentName` → the BOUND
 * sub-agent console-backend named in the handshake → the name the host itself
 * publishes at `/.well-known/agent-skills/index.json` → the A2A handshake's
 * agent name → null while nothing is known yet.
 *
 * The backend's answer outranks the page's own well-known index because the
 * binding's `base_url` is the authority and need not be the page origin: a host
 * whose SPA serves no index (an API host, a static server that 404s) would
 * otherwise fall through to the A2A card, which names the orchestrator.
 */
import { useEffect, useState, useSyncExternalStore } from 'react';
import { useChatEngine } from '../engine';
import { useEmbeddedAgent } from './use-embedded-agent';

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
  const boundName = useEmbeddedAgent()?.name ?? null;
  return adapter.agentName ?? boundName ?? hostName ?? handshakeName ?? null;
}
