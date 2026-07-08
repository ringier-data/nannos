import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type { NannosConfig, NannosCore, Settings } from '../core';

export type FeedbackRating = 'positive' | 'negative';

export interface FeedbackItem {
  message_id?: string | null;
  rating: FeedbackRating;
  [key: string]: unknown;
}

export interface UploadedFileInfo {
  uri: string;
  mimeType: string;
  name: string;
  s3Url: string;
}

/** Persisted user chat preferences (console: Settings page / auth/me/settings). */
export interface UserChatSettings {
  preferred_model?: string | null;
  preferred_model_retired?: boolean;
  effective_preferred_model?: string | null;
  enable_thinking?: boolean | null;
  thinking_level?: string | null;
  [key: string]: unknown;
}

/**
 * Host adapter — THE client-side integration contract between a host
 * application and the Nannos UI kit. Everything console-specific that
 * `ChatContext`/`ChatApp` used to reach via `@/…` imports is injected here.
 *
 * Design rules:
 * - Every field is optional; zero-config defaults reproduce console behavior
 *   where it generalizes (same-origin REST) and HIDE affordances where it
 *   doesn't (usage/trace links, settings persistence, file upload).
 * - The adapter carries *host-app* concerns only. Protocol/connection concerns
 *   (backendUrl, token, socket) live in the core's `NannosConfig`.
 */
export interface NannosHostAdapter {
  /** Override the name shown in the chat header. By default the embed auto-resolves
   *  the scoped sub-agent's name (from `subAgentId`); set this to force a label
   *  instead (or when there's no sub-agent). Falls back to the A2A handshake's
   *  agent name if neither is available. */
  agentName?: string;

  /** Host-known auth facts. The widget never derives these itself. */
  auth?: {
    /** Gates admin-only affordances (e.g. trace links). Default: false. */
    isAdmin?: boolean;
    /** True while an admin impersonates another user (console banner state). Default: false. */
    isImpersonating?: boolean;
  };

  /**
   * Deep links into host-app surfaces. Buttons render ONLY when the callback
   * is provided (console passes these; embedded hosts typically omit them).
   */
  links?: {
    /** "View usage logs" for a conversation (console: /app/usage?conversation_id=…). */
    usage?: (conversationId: string) => void;
    /** "View trace" (console: LangSmith URL). Also requires auth.isAdmin. */
    trace?: (conversationId: string) => void;
    /** Open the host's persistent settings page (console: /app). */
    openSettings?: () => void;
  };

  /**
   * Conversation-id ↔ host routing sync. Console binds this to react-router;
   * an embedded host may omit it (selection state stays internal) or bind it
   * to its own router.
   */
  routing?: {
    /** Controlled active conversation (e.g. from the host's URL). */
    activeConversationId?: string | null;
    /** Notified whenever the widget switches conversations. */
    onActiveConversationChange?: (id: string | null) => void;
    /** Is the chat surface currently visible? Suppresses new-message toasts for
     *  the conversation in view (console: pathname === '/app/chat'). Default: true. */
    isChatVisible?: () => boolean;
    /** Bring the chat surface into view (console: navigate('/app/chat')). */
    openChat?: () => void;
    /** Open an arbitrary host path (agent `navigate` client-action directives). */
    navigate?: (to: string) => void;
    /** Point the user at an on-screen object/field (agent `highlight` client-action
     *  directives): scroll it into view and/or outline it. Only the host knows how
     *  to locate its own DOM, so this must be host-provided — without it, highlight
     *  directives no-op. `target` is the registered ontology object; `field` (when
     *  present) is the specific schema field to emphasise. */
    highlight?: (target: { type: string; id: string }, field?: string) => void;
  };

  /**
   * Extra headers for every backend REST call (console: admin-mode /
   * impersonation headers). The socket-side equivalent goes through the core's
   * `customHeaders`.
   */
  requestHeaders?: () => Record<string, string>;

  /**
   * Notification override. Default: the UI kit's own toast (sonner). Provide
   * this when the host wants notifications in its own system.
   */
  notify?: (
    level: 'info' | 'success' | 'error',
    message: string,
    opts?: { description?: string; onClick?: () => void },
  ) => void;

  /**
   * Backend data access that is NOT carried by the socket. Defaults hit
   * console-backend REST relative to the core's `backendUrl` (or same-origin),
   * with cookies/bearer per the core config — i.e. console needs none of these.
   */
  api?: {
    /** Persisted user chat settings. Default: none (widget uses `defaults`). */
    getUserSettings?: () => Promise<UserChatSettings | null>;
    saveUserSettings?: (settings: Settings) => Promise<boolean>;
    /** Default: multipart POST {base}/api/v1/files/upload. */
    uploadFiles?: (conversationId: string, files: Array<{ file: Blob; name: string }>) => Promise<UploadedFileInfo[]>;
    /** Default: GET {base}/api/v1/conversations/{id}/feedback. */
    getConversationFeedback?: (conversationId: string) => Promise<FeedbackItem[]>;
    /** Default: POST {base}/api/v1/conversations/{id}/messages/{mid}/feedback. */
    submitMessageFeedback?: (conversationId: string, messageId: string, rating: FeedbackRating) => Promise<boolean>;
    /** Default: DELETE {base}/api/v1/conversations/{id}/messages/{mid}/feedback. */
    deleteMessageFeedback?: (conversationId: string, messageId: string) => Promise<boolean>;
    /** Conversation-level rating. Default: POST {base}/api/v1/conversations/{id}/feedback. */
    submitConversationFeedback?: (conversationId: string, rating: FeedbackRating, subAgentIds?: string[]) => Promise<boolean>;
    /** Bug reporting. NO default — absent hides the report-issue affordance. */
    reportIssue?: (report: { conversationId: string; messageId?: string; description?: string }) => Promise<boolean>;
    /** Live model catalog. NO default — absent uses the kit's static fallback list. */
    listModels?: () => Promise<Array<{ value: string; label: string; provider?: string; supportsThinking?: boolean; thinkingLevels?: string[] }>>;
  };

  /** Fallback Settings when no persisted user settings exist (console: config.orchestratorUrl). */
  defaults?: {
    agentUrl?: string;
    model?: string;
  };
}

/** Adapter with defaults applied — what the UI kit actually consumes. */
export interface ResolvedHostAdapter {
  agentName?: string;
  isAdmin: boolean;
  isImpersonating: boolean;
  links: NonNullable<NannosHostAdapter['links']>;
  routing: NonNullable<NannosHostAdapter['routing']>;
  notify?: NannosHostAdapter['notify'];
  api: {
    /** Authenticated fetch against the backend (base URL, credentials, token,
     *  host `requestHeaders` applied). The chat state machine builds its own
     *  query semantics on top of this primitive. */
    fetch: (path: string, init?: RequestInit) => Promise<Response>;
    getUserSettings: () => Promise<UserChatSettings | null>;
    saveUserSettings: (settings: Settings) => Promise<boolean>;
    uploadFiles: (conversationId: string, files: Array<{ file: Blob; name: string }>) => Promise<UploadedFileInfo[]>;
    getConversationFeedback: (conversationId: string) => Promise<FeedbackItem[]>;
    submitMessageFeedback: (conversationId: string, messageId: string, rating: FeedbackRating) => Promise<boolean>;
    deleteMessageFeedback: (conversationId: string, messageId: string) => Promise<boolean>;
    submitConversationFeedback: (conversationId: string, rating: FeedbackRating, subAgentIds?: string[]) => Promise<boolean>;
    reportIssue?: NonNullable<NannosHostAdapter['api']>['reportIssue'];
    listModels?: NonNullable<NannosHostAdapter['api']>['listModels'];
  };
  defaults: { agentUrl?: string; model?: string };
}

/** Base fetch against console-backend REST, honoring the core connection config. */
export async function backendFetch(config: NannosConfig, path: string, init?: RequestInit): Promise<Response> {
  const base = config.backendUrl ?? window.location.origin;
  const url = new URL(path, base);
  const headers = new Headers(init?.headers);
  if (config.getToken) headers.set('Authorization', `Bearer ${await config.getToken()}`);
  return fetch(url.toString(), { ...init, credentials: 'include', headers });
}

export function resolveHostAdapter(adapter: NannosHostAdapter, config: NannosConfig): ResolvedHostAdapter {
  const feedbackPath = (conversationId: string) => `/api/v1/conversations/${encodeURIComponent(conversationId)}/feedback`;
  const messageFeedbackPath = (conversationId: string, messageId: string) =>
    `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/feedback`;

  /** Every REST default flows through here: base URL + credentials + token + host headers. */
  const boundFetch = async (path: string, init?: RequestInit): Promise<Response> => {
    const headers = new Headers(init?.headers);
    for (const [k, v] of Object.entries(adapter.requestHeaders?.() ?? {})) headers.set(k, v);
    return backendFetch(config, path, { ...init, headers });
  };

  return {
    agentName: adapter.agentName,
    isAdmin: adapter.auth?.isAdmin ?? false,
    isImpersonating: adapter.auth?.isImpersonating ?? false,
    links: adapter.links ?? {},
    routing: adapter.routing ?? {},
    notify: adapter.notify,
    api: {
      fetch: boundFetch,
      getUserSettings: adapter.api?.getUserSettings ?? (async () => null),
      saveUserSettings: adapter.api?.saveUserSettings ?? (async () => false),
      uploadFiles:
        adapter.api?.uploadFiles ??
        (async (conversationId, files) => {
          const formData = new FormData();
          formData.append('conversation_id', conversationId);
          for (const f of files) formData.append('files', f.file, f.name);
          const resp = await boundFetch('/api/v1/files/upload', { method: 'POST', body: formData });
          if (!resp.ok) {
            const errorData = (await resp.json().catch(() => ({}))) as { detail?: unknown };
            const detail = errorData.detail;
            const message =
              typeof detail === 'string'
                ? detail
                : Array.isArray(detail)
                  ? detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join('; ')
                  : detail
                    ? JSON.stringify(detail)
                    : 'Upload failed';
            throw new Error(message);
          }
          const data = (await resp.json()) as { files: UploadedFileInfo[] };
          return data.files;
        }),
      getConversationFeedback:
        adapter.api?.getConversationFeedback ??
        (async (conversationId) => {
          const resp = await boundFetch(feedbackPath(conversationId));
          if (!resp.ok) return [];
          return (await resp.json()) as FeedbackItem[];
        }),
      submitMessageFeedback:
        adapter.api?.submitMessageFeedback ??
        (async (conversationId, messageId, rating) => {
          const resp = await boundFetch(messageFeedbackPath(conversationId, messageId), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rating }),
          });
          return resp.ok;
        }),
      deleteMessageFeedback:
        adapter.api?.deleteMessageFeedback ??
        (async (conversationId, messageId) => {
          const resp = await boundFetch(messageFeedbackPath(conversationId, messageId), { method: 'DELETE' });
          return resp.ok;
        }),
      submitConversationFeedback:
        adapter.api?.submitConversationFeedback ??
        (async (conversationId, rating, subAgentIds = []) => {
          const resp = await boundFetch(feedbackPath(conversationId), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              rating,
              ...(subAgentIds.length > 0 && { sub_agent_id: subAgentIds.join(', ') }),
            }),
          });
          return resp.ok;
        }),
      reportIssue: adapter.api?.reportIssue,
      listModels: adapter.api?.listModels,
    },
    defaults: adapter.defaults ?? {},
  };
}

interface HostAdapterContextValue {
  adapter: ResolvedHostAdapter;
  core: NannosCore | null;
}

const HostAdapterContext = createContext<HostAdapterContextValue | undefined>(undefined);

const EMPTY_CONFIG: NannosConfig = {};
/** Zero-config fallback: same-origin console-backend REST + cookie auth. */
const DEFAULT_RESOLVED = resolveHostAdapter({}, EMPTY_CONFIG);

export function HostAdapterProvider({
  core = null,
  adapter = {},
  children,
}: {
  core?: NannosCore | null;
  adapter?: NannosHostAdapter;
  children: ReactNode;
}) {
  const value = useMemo(
    () => ({ adapter: resolveHostAdapter(adapter, core?.config ?? EMPTY_CONFIG), core }),
    [adapter, core],
  );
  return <HostAdapterContext.Provider value={value}>{children}</HostAdapterContext.Provider>;
}

/** Resolved adapter; falls back to zero-config defaults when no provider is mounted. */
export function useHostAdapter(): ResolvedHostAdapter {
  return useContext(HostAdapterContext)?.adapter ?? DEFAULT_RESOLVED;
}

/** Core connection config; `{}` (same-origin, cookies) when no provider is mounted. */
export function useNannosCoreConfig(): NannosConfig {
  return useContext(HostAdapterContext)?.core?.config ?? EMPTY_CONFIG;
}

/** The full core (registry, client-action binding). Requires a provider with a core. */
export function useNannosCore(): NannosCore {
  const core = useContext(HostAdapterContext)?.core;
  if (!core) throw new Error('useNannosCore requires a HostAdapterProvider with a core');
  return core;
}

/** The core when the host provided one, else null (console runs without a core). */
export function useNannosCoreOptional(): NannosCore | null {
  return useContext(HostAdapterContext)?.core ?? null;
}
