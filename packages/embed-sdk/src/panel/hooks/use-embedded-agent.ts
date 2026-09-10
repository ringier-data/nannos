/**
 * The sub-agent this embedded surface is bound to (ADR-0006), as console-backend
 * resolved it from the connection's token `azp` and answered in the
 * `client_initialized` handshake. Null on a console session, on an unbound token,
 * and until the handshake lands.
 *
 * This is the authoritative label for an embedded surface: the A2A card in the same
 * handshake names the ORCHESTRATOR, and the host's own well-known index is served
 * from an origin the page may not share with the binding's base URL.
 */
import { useSyncExternalStore } from 'react';
import type { EmbeddedAgentInfo } from '../../core/wire';
import { useChatEngineOptional } from '../engine';

const NONE = { embeddedAgent: null } as const;

export function useEmbeddedAgent(): EmbeddedAgentInfo | null {
  const engine = useChatEngineOptional();
  const connection = engine?.connection;
  const snapshot = useSyncExternalStore(
    (listener) => connection?.subscribe(listener) ?? (() => {}),
    () => connection?.getSnapshot() ?? NONE,
    () => NONE,
  );
  return snapshot.embeddedAgent;
}
