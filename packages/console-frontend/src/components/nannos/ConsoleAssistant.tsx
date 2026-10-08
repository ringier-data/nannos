import { useEffect, useMemo, useRef } from 'react';
import { useLocation } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { Sparkles } from 'lucide-react';
import { useAssistant, type NannosHostAdapter } from '@nannos/embed-sdk';
import { AssistantDock } from '@nannos/embed-sdk/panel';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useAuth } from '@/contexts/AuthContext';
import { mainNavItems, groupManagerNavItems, adminNavItems } from '@/config/navigation';

/**
 * The console's docked assistant: the console embedding its own Nannos
 * assistant, like the cockpit does. The dock runs on its OWN embed scope —
 * console-backend binds that socket to the sub-agent whose embed binding lists
 * the console's OAuth client, and that sub-agent's definition is the one the
 * console publishes under /.well-known/agent-skills/. It acts on the forms the
 * page registered (see consoleObjects.ts). The console's main chat (/app/chat)
 * stays on the orchestrator, with its own conversations.
 */

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

/** Width of the open dock, on `<html>`; 0 (unset) while it is closed. Read by the toaster. */
export const CONSOLE_DOCK_WIDTH_VAR = '--console-dock-width';

/** Docked panel at the right edge of every console page. */
export function ConsoleAssistantDock() {
  const { isAdmin, adminMode } = useAuth();
  const { adapter, isOpen, open, panelWidth } = useAssistant();
  const isOpenRef = useRef(isOpen);
  useEffect(() => {
    isOpenRef.current = isOpen;
  }, [isOpen]);
  // The width the dock covers at the right edge while it is on screen — pinned
  // OR floating (the SDK's `--nannos-panel-width` is 0 for an overlay, because
  // the page keeps its width under it). The toaster moves out from under the
  // dock by it: a toast sitting on the composer swallows the click meant for it,
  // and what the user types next goes nowhere.
  useEffect(() => {
    const root = document.documentElement;
    if (isOpen) root.style.setProperty(CONSOLE_DOCK_WIDTH_VAR, `${panelWidth}px`);
    else root.style.removeProperty(CONSOLE_DOCK_WIDTH_VAR);
    return () => {
      root.style.removeProperty(CONSOLE_DOCK_WIDTH_VAR);
    };
  }, [isOpen, panelWidth]);
  // The dock is its own chat surface: a reply is "seen" while it is open, and a
  // reply toast opens it — not the provider's main-chat surface (/app/chat).
  const dockAdapter = useMemo<NannosHostAdapter>(
    () => ({
      ...adapter,
      chatSurface: { isVisible: () => isOpenRef.current, bringIntoView: () => open() },
    }),
    [adapter, open]
  );
  return (
    // Narrow: history opens as the header's popover, not a sidebar that would
    // squeeze the thread.
    <AssistantDock
      embedScope
      adapter={dockAdapter}
      shadow={false}
      devMode={isAdmin && adminMode}
      className="border-l bg-background"
      zIndex={40}
    />
  );
}

/** Header launcher. The click is the user gesture `open()` wants. */
export function ConsoleAssistantLauncher() {
  const { isAvailable, isOpen, open } = useAssistant();
  if (!isAvailable || isOpen) return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="outline" size="sm" onClick={() => open()} data-testid="nannos-launcher">
          <Sparkles className="h-4 w-4" />
          Assistant
        </Button>
      </TooltipTrigger>
      <TooltipContent>{isMac ? '⌘J' : 'Ctrl+J'}</TooltipContent>
    </Tooltip>
  );
}

const NAV_ITEMS = [...mainNavItems, ...groupManagerNavItems, ...adminNavItems];

/** The nav entry a path belongs to: the longest matching url, so /app doesn't swallow everything. */
function pageTitle(pathname: string): string | undefined {
  let best: { title: string; url: string } | undefined;
  for (const item of NAV_ITEMS) {
    const matches = pathname === item.url || pathname.startsWith(`${item.url}/`);
    if (matches && (!best || item.url.length > best.url.length)) best = item;
  }
  return best?.title;
}

/**
 * Base page-context layer: which console page the user is on. Pages layer the
 * entity they show on top with `useNannosPageContext`.
 */
export function ConsolePageContextBridge() {
  const { pathname, hash } = useLocation();
  const { setPageContext, core } = useAssistant();
  const queryClient = useQueryClient();
  const { isAdmin, adminMode } = useAuth();
  useEffect(() => {
    const title = pageTitle(pathname);
    // Admin pages need admin mode on; without it in view the agent navigated to one,
    // the route guard bounced the user to Settings, and only then did it explain.
    setPageContext({
      key: `${pathname}${hash}`,
      ...(title ? { title } : {}),
      ...(isAdmin ? { view: { admin_mode: adminMode ? 'on' : 'off' } } : {}),
    });
  }, [pathname, hash, setPageContext, isAdmin, adminMode]);
  // Every page can be refreshed: after the assistant changes something with a server
  // tool, what the page shows is stale, and refetching its data is what a reload would do
  // — without losing the dock or an open form's typed values (forms keep their state).
  useEffect(() => {
    if (!core) return;
    const handle = core.register({
      type: 'Page',
      id: pathname,
      scope: 'view',
      label: pageTitle(pathname) ?? pathname,
      getState: () => ({}),
      apply: (values) => ({
        applied: [],
        rejected: Object.keys(values as Record<string, unknown>).map((field) => ({ field, reason: 'not a form' })),
      }),
      actions: {
        refresh: {
          label: 'Refresh',
          description:
            'Refetch the data this page shows, e.g. after a server tool changed something on it. ' +
            'Refused while a form here holds unsaved edits.',
          // Not over unsaved edits: the agent refreshed a job page while the user's typed
          // name sat in the form, then described the job by that unsaved name. The page
          // keeps the form values, but what it shows around them would no longer match.
          run: async () => {
            const unsaved = [
              ...core.changes.pending().map((u) => `${u.target.type}:${u.target.id}`),
              ...core.registry.dirty(),
            ].filter((key, i, all) => all.indexOf(key) === i);
            if (unsaved.length) {
              return {
                ok: false,
                detail:
                  `Not refreshed: ${unsaved.join(', ')} holds unsaved edits. Tell the user the page shows ` +
                  'the old state until they save or discard them.',
              };
            }
            await queryClient.invalidateQueries();
          },
        },
      },
    });
    return () => handle.dispose();
  }, [core, pathname, queryClient]);
  return null;
}
