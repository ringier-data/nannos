import { useEffect, useMemo, useRef, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { NannosProvider, useAssistant, type NannosHostAdapter } from '@nannos/embed-sdk';
import { NannosChatScope } from '@nannos/embed-sdk/panel';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import { config } from '@/config';
import { getCurrentUserSettingsApiV1AuthMeSettingsGet, createBugReportApiV1BugReportsPost } from '@/api/generated';
import { listAvailableModels } from '@/api/model-gateway';
import {
  getAdminModeFromStorage,
  getImpersonatedUserIdFromStorage,
  ADMIN_MODE_HEADER,
  IMPERSONATE_USER_HEADER,
} from '@/api/apiInstanceConfig';

/**
 * Console's Nannos wiring (embed-sdk v2): ONE `<NannosProvider>` (same-origin
 * socket + cookie auth — an empty config) with the console host adapter, plus
 * the DEFAULT chat scope mounted at the layout so streaming, unread counts and
 * reply toasts survive navigation between pages; `<AssistantPanel>` on the
 * chat page reuses this scope.
 *
 * The adapter carries react-router navigation, LangSmith trace links,
 * impersonation/admin request headers, generated-API user settings + bug
 * reports, and the Model Gateway catalog. The SDK's zero-config REST defaults
 * (same-origin console-backend) cover the rest.
 */
export function ConsoleNannosProvider({ children }: { children: ReactNode }) {
  const { isAdmin, isImpersonating } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const locationRef = useRef(location);
  useEffect(() => {
    locationRef.current = location;
  }, [location]);

  const adapter = useMemo<NannosHostAdapter>(
    () => ({
      auth: { isAdmin, isImpersonating },
      links: {
        usage: (conversationId) => navigate(`/app/usage?conversation_id=${conversationId}`),
        trace: (conversationId) =>
          window.open(
            `https://eu.smith.langchain.com/o/${config.langsmith.organizationId}/projects/p/${config.langsmith.projectId}/t/${conversationId}`,
            '_blank',
            'noopener,noreferrer'
          ),
        openSettings: () => navigate('/app'),
      },
      chatSurface: {
        isVisible: () => locationRef.current.pathname === '/app/chat',
        bringIntoView: () => navigate('/app/chat'),
      },
      notify: (level, message, opts) =>
        toast[level === 'error' ? 'error' : level === 'success' ? 'success' : 'info'](message, {
          description: opts?.description,
          ...(opts?.onClick && { action: { label: 'Open', onClick: opts.onClick } }),
        }),
      requestHeaders: () => {
        const headers: Record<string, string> = {};
        const impersonatedUserId = getImpersonatedUserIdFromStorage();
        if (impersonatedUserId) {
          headers[IMPERSONATE_USER_HEADER] = impersonatedUserId;
          headers[ADMIN_MODE_HEADER] = 'true'; // Force admin mode when impersonating
        } else if (getAdminModeFromStorage()) {
          headers[ADMIN_MODE_HEADER] = 'true';
        }
        return headers;
      },
      api: {
        getUserSettings: async () => {
          const res = await getCurrentUserSettingsApiV1AuthMeSettingsGet();
          return (res.data as { data?: Record<string, unknown> } | undefined)?.data ?? null;
        },
        reportIssue: async ({ conversationId, messageId, description }) => {
          const res = await createBugReportApiV1BugReportsPost({
            body: {
              conversation_id: conversationId,
              message_id: messageId,
              description,
              source: 'client',
            },
          });
          return !res.error;
        },
        listModels: async () =>
          (await listAvailableModels()).map((m) => ({
            value: m.value,
            label: m.label,
            provider: m.provider,
            supportsThinking: m.supports_thinking,
            thinkingLevels: m.thinking_levels ?? undefined,
          })),
      },
      defaults: { agentUrl: config.orchestratorUrl },
    }),
    [isAdmin, isImpersonating, navigate]
  );

  return (
    <NannosProvider
      config={{}}
      adapter={adapter}
      navigate={(to) => navigate(to)}
      // The console's chat is a full page, not a togglable panel — no shortcut,
      // and the pin/width machinery stays dormant.
      shortcut={false}
      storagePrefix="console-nannos"
    >
      <NannosChatScope>
        <DemoClientActionRegistration />
        {children}
      </NannosChatScope>
    </NannosProvider>
  );
}

/**
 * Dev-only demo of the client-action loop (Embedded Nannos). Enabled with
 * `localStorage['nannos-embed-demo'] = '1'` + reload: registers a fake
 * on-screen object on the provider core so the agent's `client_action`
 * directives can be observed end-to-end in the console (toast + console.log).
 * Inert otherwise — with no registered objects, no manifest is sent and no
 * directive can ever target anything.
 */
function DemoClientActionRegistration() {
  const core = useAssistant().core;
  useEffect(() => {
    if (!core) return;
    try {
      if (window.localStorage.getItem('nannos-embed-demo') !== '1') return;
    } catch {
      return;
    }
    const state: Record<string, unknown> = { title: '', body: '' };
    (window as unknown as Record<string, unknown>).__nannosDemoState = state;
    const handle = core.register({
      type: 'DemoNote',
      id: '1',
      scope: 'update',
      label: 'Demo note form',
      fields: ['title', 'body'],
      getState: () => state,
      apply: (values) => {
        Object.assign(state, values);
        (window as unknown as Record<string, unknown>).__nannosLastApply = { ...values };
        console.log('[NANNOS-DEMO] client-action apply received:', values);
        toast.success('Nannos filled the demo form', { description: JSON.stringify(values) });
        return { applied: Object.keys(values), rejected: [] };
      },
    });
    console.log('[NANNOS-DEMO] Demo object registered (DemoNote#1)');
    return () => handle.dispose();
  }, [core]);
  return null;
}
