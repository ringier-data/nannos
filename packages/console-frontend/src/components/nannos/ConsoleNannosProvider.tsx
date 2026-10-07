import { useEffect, useMemo, useRef, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';
import {
  NannosProvider,
  createClientActionHandlers,
  resolveHighlightLabel,
  type NannosHostAdapter,
} from '@nannos/embed-sdk';
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
import { ConsolePageContextBridge } from './ConsoleAssistant';
import { consoleObjectTypes } from './consoleObjects';

/**
 * Console's Nannos wiring (embed-sdk v2): ONE `<NannosProvider>` (same-origin
 * socket + cookie auth — an empty config) with the console host adapter, plus
 * the DEFAULT chat scope mounted at the layout so streaming, unread counts and
 * reply toasts survive navigation between pages; `<AssistantPanel>` on the chat
 * page reuses this scope. The docked assistant (ConsoleAssistant.tsx) runs on its
 * own embed scope, bound to the console's published sub-agent.
 *
 * The console is also an embedding host of its own: its forms register as
 * client objects (consoleObjects.ts), so the assistant reads what is on screen
 * and fills them through `apply` (no approval: nothing is saved, the changed fields
 * are marked). Saving is `submit`, which the user approves — it runs the form's own
 * Save handler, passed to `<NannosForm submit>`.
 * `navigate`/`highlight` are the SDK's generic handlers on react-router.
 *
 * The adapter carries react-router navigation, LangSmith trace links,
 * impersonation/admin request headers, generated-API user settings + bug
 * reports, and the Model Gateway catalog. The SDK's zero-config REST defaults
 * (same-origin console-backend) cover the rest.
 */
export function ConsoleNannosProvider({ children }: { children: ReactNode }) {
  const { isAdmin, isImpersonating, adminMode } = useAuth();
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

  const clientActions = useMemo(
    () =>
      createClientActionHandlers({
        // Refused here, not bounced by the route guard: told admin_mode was off, the agent
        // still opened an admin page and the user landed on Settings, away from their page.
        navigate: (to) => {
          if (/^\/app\/admin(\/|$|\?|#)/.test(to) && !(isAdmin && adminMode)) {
            return isAdmin
              ? 'Admin pages need Admin Mode on: ask the user to switch Admin Mode on in the sidebar, then try again.'
              : 'Admin pages are only for administrators; this user cannot open them.';
          }
          navigate(to);
        },
        resolveFieldLabel: (type, field) => resolveHighlightLabel(consoleObjectTypes, type, field),
      }),
    [navigate, isAdmin, adminMode]
  );

  return (
    <NannosProvider
      config={{}}
      adapter={adapter}
      navigate={clientActions.navigate}
      highlight={clientActions.highlight}
      beforeApply={clientActions.beforeApply}
      markChanged={clientActions.markChanged}
      clearChanged={clientActions.clearChanged}
      onApplyResult={(_target, { rejected }) => {
        if (!rejected.length) return;
        toast.warning('The assistant could not fill every field', {
          description: rejected.map((r) => r.field).join(', '),
        });
      }}
      storagePrefix="console-nannos"
    >
      <NannosChatScope>
        <ConsolePageContextBridge />
        {children}
      </NannosChatScope>
    </NannosProvider>
  );
}

