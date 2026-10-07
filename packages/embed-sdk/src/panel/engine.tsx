/**
 * The chat-engine scope: builds and owns the per-surface engine bundle —
 * socket client, connection handshake, `A2AChatTransport`, conversation list,
 * and the `Chat` instance registry — and hands it to the panel hooks.
 *
 * One scope per chat surface. The default scope reuses the provider core's
 * transport (single authenticated socket). A scope with `customHeaders` or
 * `playground` (console's sub-agent playground) gets its OWN socket, exactly
 * like the old per-provider `SocketProvider` behavior.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { Chat } from '@ai-sdk/react';
import { TransportClient, generateUUID } from '../core';
import type { NannosCore, NannosPageContext } from '../core';
import type { Settings } from '../core/wire';
import {
  A2AChatTransport,
  ConnectionStore,
  ConversationsStore,
  WireLog,
  WireStore,
  lastAssistantMessageIsCompleteWithApprovalResponses,
  type NannosUIMessage,
  type SendContext,
} from '../transport';
import {
  backendFetch,
  resolveHostAdapter,
  useAssistant,
  type NannosHostAdapter,
  type ResolvedHostAdapter,
  type UserChatSettings,
} from '../react';
import { ComposerFocusSignal } from './composer-focus';
import { resolveDevMode, useDevModeControls } from './dev-mode';

export interface PlaygroundMode {
  /** The version hash of the sub-agent config being tested. */
  subAgentConfigHash: string;
  /** Human-readable name for display. */
  subAgentName: string;
}

export interface ChatSettings {
  model?: string;
  enableThinking?: boolean;
  thinkingLevel?: string;
}

export interface ChatEngine {
  core: NannosCore;
  client: TransportClient;
  connection: ConnectionStore;
  transport: A2AChatTransport;
  conversations: ConversationsStore;
  /** "Focus the composer" intent, fired by the new-chat buttons. */
  composerFocus: ComposerFocusSignal;
  adapter: ResolvedHostAdapter;
  sessionId: string;
  playground?: PlaygroundMode;
  /** Raw socket traffic (ring buffer) for the dev-mode inspector. */
  wireLog: WireLog;
  /** Current model/thinking selection (local override over persisted settings). */
  getSettings: () => ChatSettings;
  setSettings: (patch: Partial<ChatSettings>) => void;
  /** Chat instances by conversation id — held OUTSIDE React so background
   *  conversations keep streaming and a panel remount reattaches. */
  getOrCreateChat: (conversationId: string) => Chat<NannosUIMessage>;
  peekChat: (conversationId: string) => Chat<NannosUIMessage> | undefined;
}

const ChatEngineContext = createContext<ChatEngine | null>(null);

export function useChatEngine(): ChatEngine {
  const engine = useContext(ChatEngineContext);
  if (!engine) throw new Error('useChatEngine must be used inside <NannosChatScope> (or <AssistantPanel>).');
  return engine;
}

/** The surrounding engine, or null — for hooks that can fall back to the
 *  provider core's socket (e.g. useSocketEvent outside any chat surface). */
export function useChatEngineOptional(): ChatEngine | null {
  return useContext(ChatEngineContext);
}

function useSessionId(): string {
  return useMemo(() => {
    const KEY = 'a2a-session-id';
    try {
      let value = localStorage.getItem(KEY);
      if (!value) {
        value = generateUUID();
        localStorage.setItem(KEY, value);
      }
      return value;
    } catch {
      return generateUUID();
    }
  }, []);
}

export interface NannosChatScopeProps {
  children: ReactNode;
  /** Extra socket-init headers → this scope gets its OWN socket (playground). */
  customHeaders?: Record<string, string>;
  /** Console sub-agent playground: scopes conversations + tags every send. */
  playground?: PlaygroundMode;
  /** The console's own embedded assistant (cookie session): an own socket that asks
   *  console-backend to bind it like an embedded host (`NannosConfig.embedScope`).
   *  Its conversations are the bound agent's — editable here, listed apart from the
   *  console's main chat, resumed under their own key. */
  embedScope?: boolean;
  /** With nothing to resume: console adopts the most recent conversation,
   *  embedded surfaces start fresh. Default: true for the console's own
   *  cookie session, false for an embedded host (`core.isEmbedded()`). Every
   *  surface first tries the conversation this browser tab was on, so a reload
   *  changes nothing. */
  autoSelectConversation?: boolean;
  /** Host adapter override (defaults to the provider's). */
  adapter?: NannosHostAdapter;
}

export function NannosChatScope(props: NannosChatScopeProps): ReactNode {
  const { children, customHeaders, playground, embedScope, adapter: adapterProp } = props;
  const assistant = useAssistant();
  const existing = useContext(ChatEngineContext);
  const core = assistant.core;
  // Nesting a DEFAULT scope inside an existing one (a host mounts the scope at
  // its layout for cross-page streaming/toasts, then <AssistantPanel> renders
  // inside it) reuses the surrounding engine — two engines on one socket would
  // split the conversation list and double the handshake. A playground/custom-
  // headers scope always creates its own (own socket by design).
  if (existing && !customHeaders && !playground && !embedScope) {
    return children;
  }
  if (!core) {
    // Disabled/absent provider: render nothing rather than a dead chat shell.
    return null;
  }
  return (
    <ChatScopeInner
      {...props}
      core={core}
      adapter={adapterProp ?? assistant.adapter}
      pageContext={assistant.pageContext}
      key={playground?.subAgentConfigHash ?? (embedScope ? 'embed-scope' : 'default')}
    >
      {children}
    </ChatScopeInner>
  );
}

function ChatScopeInner({
  children,
  customHeaders,
  playground,
  embedScope,
  autoSelectConversation,
  core,
  adapter,
  pageContext,
}: NannosChatScopeProps & {
  core: NannosCore;
  adapter?: NannosHostAdapter;
  pageContext: NannosPageContext | null;
}): ReactNode {
  const sessionId = useSessionId();
  const settingsRef = useRef<ChatSettings>({});
  const [, setSettingsVersion] = useState(0);
  // Send-time read of the LIVE page context (same ref pattern as settings):
  // the memoized engine must not rebuild on navigation.
  const pageContextRef = useRef<NannosPageContext | null>(pageContext);
  pageContextRef.current = pageContext;
  // Dev mode decides whether the wire log is PERSISTED: raw payloads have no
  // business in an end user's localStorage. `available` covers the panel's
  // prop; a bare scope (no <AssistantPanel> above) still honors the
  // localStorage escape hatch. Read once — neither source flips at runtime.
  const persistWire = useDevModeControls().available || resolveDevMode();

  const engine = useMemo<ChatEngine>(() => {
    const ownSocket = !!customHeaders || !!playground || !!embedScope;
    const client = ownSocket
      ? new TransportClient({
          ...core.config,
          customHeaders: { ...core.config.customHeaders, ...customHeaders },
          ...(embedScope && { embedScope: true }),
        })
      : core.transport;

    const resolved = resolveHostAdapter(adapter ?? {}, core.config);
    const fetcher = resolved.api.fetch;

    const connection = new ConnectionStore(
      client,
      async (): Promise<Settings> => {
        // Agent URL: explicit adapter default → discovery from the backend.
        const discovered =
          resolved.defaults.agentUrl ?? (await core.resolveAgentUrl(fetcher)) ?? '';
        const s = settingsRef.current;
        return {
          agentUrl: discovered,
          model: s.model ?? resolved.defaults.model ?? '',
          ...(s.enableThinking !== undefined && { enableThinking: s.enableThinking }),
          ...(s.thinkingLevel && { thinkingLevel: s.thinkingLevel }),
        };
      },
      sessionId,
    );

    const embedded = !playground && (!!embedScope || core.isEmbedded());
    const conversations = new ConversationsStore({
      fetch: fetcher,
      embedded,
      ...(embedScope && {
        getEmbeddedSubAgentId: () => connection.getSnapshot().embeddedAgent?.subAgentId,
        // State-driven, not `whenReady()`: a first handshake attempt can fail before
        // this scope's own socket is up, and the retry that succeeds settles nobody.
        whenScoped: () => whenInitialized(connection, core.config.initTimeoutMs ?? 15_000),
      }),
      subAgentConfigHash: playground?.subAgentConfigHash,
      getAgentUrl: () => resolved.defaults.agentUrl,
      autoSelectConversation: autoSelectConversation ?? !embedded,
    });

    const getSendContext = (): SendContext => ({
      sessionId,
      model: settingsRef.current.model ?? resolved.defaults.model,
      ...(settingsRef.current.enableThinking !== undefined && {
        enableThinking: settingsRef.current.enableThinking,
      }),
      ...(settingsRef.current.thinkingLevel && { thinkingLevel: settingsRef.current.thinkingLevel }),
      ...(core.manifest().length > 0 && { clientObjects: core.manifest() }),
      ...(pageContextRef.current && { pageContext: pageContextRef.current }),
      ...(playground && {
        subAgentConfigHash: playground.subAgentConfigHash,
        playgroundSubagentName: playground.subAgentName,
      }),
    });

    const composerFocus = new ComposerFocusSignal();

    const wireLog = new WireLog(persistWire ? new WireStore() : undefined);
    const transport = new A2AChatTransport({
      client,
      whenReady: () => connection.whenReady(),
      getSendContext,
      wireLog,
      onTurnEvent: (e) => {
        if (e.type === 'reconcile') {
          // The turn ended while we were away — the Chat seeds/refetches on
          // next attach; the list still needs its preview refreshed.
          void conversations.loadList();
          return;
        }
        conversations.noteActivity(e.conversationId, e.preview);
        const surface = resolved.chatSurface;
        const visible = surface.isVisible ? surface.isVisible() : true;
        if (e.type === 'finished' && (!visible || e.conversationId !== conversations.activeId)) {
          resolved.notify?.('info', e.preview?.slice(0, 80) || 'The assistant replied', {
            onClick: () => surface.bringIntoView?.(),
          });
        }
      },
    });

    const chats = new Map<string, Chat<NannosUIMessage>>();
    const getOrCreateChat = (conversationId: string): Chat<NannosUIMessage> => {
      let chat = chats.get(conversationId);
      if (!chat) {
        chat = new Chat<NannosUIMessage>({
          id: conversationId,
          transport,
          messages: [],
          sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithApprovalResponses,
        });
        chats.set(conversationId, chat);
      }
      return chat;
    };

    return {
      core,
      client,
      connection,
      transport,
      conversations,
      composerFocus,
      adapter: resolved,
      sessionId,
      playground,
      wireLog,
      getSettings: () => settingsRef.current,
      setSettings: (patch) => {
        settingsRef.current = { ...settingsRef.current, ...patch };
        setSettingsVersion((v) => v + 1);
      },
      getOrCreateChat,
      peekChat: (id) => chats.get(id),
    };
    // Scope identity: a new core / playground hash remounts via the key above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [core, sessionId, playground?.subAgentConfigHash]);

  // Own-socket scopes manage their connection lifecycle; the default scope's
  // socket belongs to <NannosProvider>.
  useEffect(() => {
    const ownSocket = engine.client !== engine.core.transport;
    if (ownSocket) void engine.client.connect();
    // Re-arm first: React runs mount → cleanup → mount over the SAME memoized
    // engine (StrictMode, Fast Refresh), so the cleanup's detach must be undone
    // before anything else here — a still-detached transport rejects every send
    // ("Not connected to the assistant backend.") behind a healthy socket.
    engine.transport.attach();
    engine.connection.attach();
    // The backend names a conversation a moment after its first exchange ends
    // and pushes the result to the conversation's room; without this the list and
    // the header would keep the first-message placeholder until the next load.
    const offTitles = engine.client.onConversationUpdated((update) => {
      engine.conversations.applyServerTitle(update.conversationId, update);
    });
    // An own socket's navigate/highlight directives reach the host hooks only this way.
    const offActions = ownSocket ? engine.core.routeClientActions(engine.client) : () => {};
    // An embed scope's list is filtered by the agent the handshake binds — list after it.
    void engine.connection.initialize();
    void engine.conversations.loadList();
    // Seed persisted user settings once (model/thinking preferences).
    void engine.adapter.api.getUserSettings().then((settings: UserChatSettings | null) => {
      if (!settings) return;
      engine.setSettings({
        ...(settings.preferred_model && { model: settings.preferred_model }),
        ...(settings.enable_thinking != null && { enableThinking: settings.enable_thinking }),
        ...(settings.thinking_level && { thinkingLevel: settings.thinking_level }),
      });
    });
    return () => {
      offTitles();
      offActions();
      engine.transport.destroy();
      engine.connection.destroy();
      if (ownSocket) engine.client.disconnect();
    };
  }, [engine]);

  return <ChatEngineContext.Provider value={engine}>{children}</ChatEngineContext.Provider>;
}

/** Resolves once the connection is initialized, or after `timeoutMs` (fail open). */
function whenInitialized(connection: ConnectionStore, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    if (connection.getSnapshot().initialized) return resolve();
    const timer = setTimeout(done, timeoutMs);
    const off = connection.subscribe(() => {
      if (connection.getSnapshot().initialized) done();
    });
    function done() {
      clearTimeout(timer);
      off();
      resolve();
    }
  });
}

export { backendFetch };
