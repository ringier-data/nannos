import { useEffect, useMemo, useRef, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { createNannos, HostAdapterProvider, type NannosCore, type NannosHostAdapter } from '@nannos/embed-sdk';
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
 * Console's implementation of the Embed SDK host adapter: react-router
 * navigation, LangSmith trace links, impersonation/admin request headers,
 * generated-API user settings + bug reports, and the Model Gateway catalog.
 * The SDK's zero-config REST defaults (same-origin console-backend) cover the
 * rest. No `core` is passed: same-origin socket + cookie auth.
 */
/**
 * Dev-only demo of the client-action loop (Embedded Nannos). Enabled with
 * `localStorage['nannos-embed-demo'] = '1'` + reload: registers a fake
 * on-screen object so the agent's `client_action` directives can be observed
 * end-to-end in the console (toast + console.log). Inert otherwise — the
 * console registers no objects, so no manifest is sent and no directive can
 * ever target anything.
 */
function createDemoCore(): NannosCore | null {
  try {
    if (window.localStorage.getItem('nannos-embed-demo') !== '1') return null;
  } catch {
    return null;
  }
  const core = createNannos({});
  const state: Record<string, unknown> = { title: '', body: '' };
  (window as unknown as Record<string, unknown>).__nannosDemoState = state;
  core.register({
    type: 'DemoNote',
    id: '1',
    scope: 'update',
    label: 'Demo note form',
    fields: ['title', 'body'],
    getState: () => state,
    apply: (values) => {
      Object.assign(state, values);
      // Instance-proof marker (StrictMode may double-create the demo core).
      (window as unknown as Record<string, unknown>).__nannosLastApply = { ...values };
      console.log('[NANNOS-DEMO] client-action apply received:', values);
      toast.success('Nannos filled the demo form', { description: JSON.stringify(values) });
    },
  });
  console.log('[NANNOS-DEMO] Demo object registered (DemoNote#1)');
  return core;
}

export function ConsoleHostAdapterProvider({ children }: { children: ReactNode }) {
  const { isAdmin, isImpersonating } = useAuth();
  const demoCore = useMemo(createDemoCore, []);
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
      routing: {
        isChatVisible: () => locationRef.current.pathname === '/app/chat',
        openChat: () => navigate('/app/chat'),
      },
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
    <HostAdapterProvider core={demoCore} adapter={adapter}>
      {children}
    </HostAdapterProvider>
  );
}
