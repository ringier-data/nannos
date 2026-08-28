/**
 * `<AssistantPanel>` — the complete chat surface: engine scope, optional
 * shadow-root style isolation, optional conversation sidebar, header, thread,
 * HITL approvals, live work plan, composer and connection footer.
 */
import { useState, type ReactNode } from 'react';
import {
  DownloadIcon,
  HistoryIcon,
  MessageCirclePlusIcon,
  PanelLeftOpenIcon,
  PanelRightCloseIcon,
  PinIcon,
  PinOffIcon,
} from 'lucide-react';
import { Button } from '../components/ui/button';
import { cn } from '../lib/utils';
import { useAssistant, useStrings, type NannosHostAdapter } from '../react';
import { NannosChatScope, type PlaygroundMode } from './engine';
import { ApplyModeProvider, type ApplyMode } from './apply-mode';
import { SendModeProvider } from './send-mode';
import { downloadTextFile, formatTranscript, slugifyFilename } from './transcript';
import { ShadowPortal } from './shadow-portal';
import { useConversations } from './hooks/use-conversations';
import { useNannosChat, type UseNannosChatValue } from './hooks/use-nannos-chat';
import { ApprovalCard } from './components/approval-card';
import { Composer } from './components/composer';
import { DevContextInspector } from './components/dev-context-inspector';
import { DevModeProvider, resolveDevMode, useDevModeControls } from './dev-mode';
import { PAGE_COLUMN, PanelLayoutProvider, usePanelLayout, type PanelLayout } from './layout';
import { ConnectionStatus } from './components/connection-status';
import {
  ConversationHistoryOverlay,
  ConversationHistoryProvider,
  useConversationHistory,
} from './components/conversation-history';
import { ConversationList } from './components/conversation-list';
import { Thread } from './components/thread';
import { WorkingBlock } from './components/working-block';

export interface AssistantPanelProps {
  /** Render inside a shadow root (style isolation). Default: true. */
  shadow?: boolean;
  /** Classes for the shadow HOST element (e.g. 'dark'). Shadow mode only. */
  hostClassName?: string;
  /** Host override stylesheets, adopted after the SDK sheet. Shadow mode only. */
  styles?: string[];
  /** `false` = no header; a ReactNode replaces the default header. */
  header?: ReactNode | false;
  /**
   * Show the conversation list as a permanent sidebar. Default: false — a narrow
   * panel reaches the same list through the header's history button instead.
   */
  showConversationList?: boolean;
  /** Classes for the panel's inner container. */
  className?: string;
  /** Extra socket-init headers → this panel gets its own socket. */
  customHeaders?: Record<string, string>;
  /** Console sub-agent playground mode. */
  playground?: PlaygroundMode;
  /** Host adapter override (defaults to the provider's). */
  adapter?: NannosHostAdapter;
  /**
   * Developer mode: show a live inspector of what the host pushes to the agent
   * (page context, conversation contextKey, client-object manifest). Unset,
   * `localStorage['nannos:dev'] = '1'` enables it without a rebuild.
   */
  devMode?: boolean;
  /**
   * How much the assistant may do to a form on its own — see `apply-mode.tsx`.
   * `'manual'` asks before every fill; `'allow-edits'` lets the panel answer
   * for the user. Set → the host decides and the header shows no control;
   * unset → the viewer chooses, remembered in this browser.
   */
  applyMode?: ApplyMode;
  /**
   * `'panel'` (default) is the narrow docked surface every SDK style was tuned
   * for. `'page'` is a full-width host page: the thread and composer share a
   * centred reading column, and each turn's activity lines fold into one
   * disclosure instead of a loose grey stream.
   */
  layout?: PanelLayout;
}

/**
 * The conversation's display name: what changes as the user moves through
 * their chats (the agent's identity is the host's own chrome to show). An
 * unnamed conversation — brand new, or waiting for the backend's written title
 * — reads as "New conversation" in the user's language.
 */
function useConversationTitle(): string {
  const strings = useStrings();
  const { conversations, activeConversationId } = useConversations();
  const active = conversations.find((c) => c.id === activeConversationId);
  return active?.title || strings['thread.newConversation'];
}

/** Download the loaded thread as a text transcript. Renders nothing on an empty thread. */
function ExportButton({ chat, title }: { chat: UseNannosChatValue; title: string }) {
  const strings = useStrings();
  if (chat.messages.length === 0) return null;
  const exportConversation = () => {
    // Older pages the user never scrolled back to are genuinely missing; the
    // transcript says so rather than looking complete.
    const text = formatTranscript(title, chat.messages, {
      truncated: chat.hasOlderMessages,
      labels: {
        user: strings['export.user'],
        assistant: strings['export.assistant'],
        truncated: strings['export.truncated'],
      },
    });
    downloadTextFile(`${slugifyFilename(title)}.txt`, text);
  };
  return (
    <Button
      data-slot="nannos-panel-export"
      type="button"
      variant="ghost"
      size="icon-sm"
      aria-label={strings['panel.export']}
      title={strings['panel.export']}
      onClick={exportConversation}
    >
      <DownloadIcon />
    </Button>
  );
}

/**
 * What a headerless page keeps of the header: the conversation's name and the
 * export action, in the thread's own reading column. Everything else the header
 * carried (pin, close, history, new chat) is the host's chrome or the sidebar's
 * on a page.
 */
function PageToolbar({ chat, className }: { chat: UseNannosChatValue; className?: string }) {
  const title = useConversationTitle();
  if (chat.messages.length === 0) return null;
  return (
    <div
      data-slot="nannos-page-toolbar"
      className={cn('flex shrink-0 items-center gap-1 pt-3 text-muted-foreground', className)}
    >
      <span className="min-w-0 flex-1 truncate text-xs">{title}</span>
      <ExportButton chat={chat} title={title} />
    </div>
  );
}

function PanelHeader({ showNewChat, chat }: { showNewChat: boolean; chat: UseNannosChatValue }) {
  const strings = useStrings();
  const assistant = useAssistant();
  const { createConversation } = useConversations();
  const history = useConversationHistory();
  const title = useConversationTitle();

  return (
    <div data-slot="nannos-panel-header" className="flex shrink-0 items-center gap-1 border-b px-3 py-2">
      <span className="min-w-0 flex-1 truncate font-medium text-sm">{title}</span>
      <ExportButton chat={chat} title={title} />
      {assistant.isAvailable && assistant.canChangePinMode && (
        <Button
          data-slot="nannos-pin-toggle"
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={assistant.isPinned ? strings['panel.unpin'] : strings['panel.pin']}
          onClick={assistant.togglePinned}
        >
          {assistant.isPinned ? <PinOffIcon /> : <PinIcon />}
        </Button>
      )}
      {/* One way in, never two: in sidebar mode the list carries this button,
          right where the user is already reading their chats. */}
      {showNewChat && (
        <Button
          data-slot="nannos-panel-new-chat"
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={strings['panel.newChat']}
          onClick={() => {
            createConversation();
            // The fresh thread must be visible: close the history popover with it.
            history.close();
          }}
        >
          <MessageCirclePlusIcon />
        </Button>
      )}
      {history.available && (
        <Button
          data-slot="nannos-panel-history"
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={strings['panel.history']}
          aria-haspopup="dialog"
          aria-expanded={history.isOpen}
          onClick={history.toggle}
        >
          <HistoryIcon />
        </Button>
      )}
      <Button
        data-slot="nannos-panel-close"
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={strings['panel.close']}
        onClick={assistant.close}
      >
        <PanelRightCloseIcon />
      </Button>
    </div>
  );
}

/** Inner component so `useNannosChat` runs INSIDE the scope. */
function PanelMain({
  header,
  hasSidebar,
}: {
  header?: ReactNode | false;
  /** The permanent conversation sidebar is on screen next to this. */
  hasSidebar: boolean;
}) {
  const chat = useNannosChat();
  // AVAILABLE, not active: the inspector's own bar carries the on/off switch,
  // so it stays mounted while the developer previews the end-user view — a
  // header the host may have removed cannot be the only way back.
  const devAvailable = useDevModeControls().available;
  const strings = useStrings();
  const layout = usePanelLayout();
  // The sidebar owns both the list and the new-chat button; the narrow panel's
  // header owns both instead.
  const historyAvailable = !hasSidebar;
  // Page layout: everything under the thread sits in the same reading column
  // the thread uses, so the composer lines up with the answers above it.
  const column = layout === 'page' ? `${PAGE_COLUMN} pb-4` : undefined;

  return (
    <ConversationHistoryProvider available={historyAvailable}>
      {/* `panel.title` names the whole surface ("Assistant") — the header inside
          it names the conversation, which is what changes as the user navigates. */}
      <div
        role="region"
        aria-label={strings['panel.title']}
        className="flex min-h-0 min-w-0 flex-1 flex-col"
      >
        {header === false ? (
          layout === 'page' && <PageToolbar chat={chat} className={PAGE_COLUMN} />
        ) : header === undefined ? (
          <PanelHeader showNewChat={historyAvailable} chat={chat} />
        ) : (
          header
        )}
        {/* The history popover hangs from the top-right of THIS box, so it
            drops out of the header button without covering the thread. */}
        <div className="relative flex min-h-0 flex-1 flex-col">
          {/* `historyAvailable` is false in sidebar mode — where the list is
              already permanent, so the thread offers no second way in. */}
          <Thread chat={chat} className="min-h-0 flex-1" showContinue={historyAvailable} />
          <div className={cn('flex shrink-0 flex-col', column)}>
            {chat.interrupt.pending.length > 0 && (
              <ApprovalCard interrupt={chat.interrupt} className="mx-2 mb-2 shrink-0" />
            )}
            {chat.isBusy && chat.workingSteps.length > 0 && (
              <WorkingBlock todos={chat.workingSteps} className="mx-2 mb-2 shrink-0" />
            )}
            {devAvailable && <DevContextInspector chat={chat} className="shrink-0" />}
            <Composer chat={chat} className="shrink-0" />
          </div>
          <ConversationHistoryOverlay />
        </div>
        <ConnectionStatus className="shrink-0 border-t" />
      </div>
    </ConversationHistoryProvider>
  );
}

const SIDEBAR_KEY = 'nannos:sidebar';

/** Remembered per browser; storage may be unavailable (private mode, sandbox). */
function readSidebarCollapsed(): boolean {
  try {
    return globalThis.localStorage?.getItem(SIDEBAR_KEY) === 'collapsed';
  } catch {
    return false;
  }
}

function writeSidebarCollapsed(collapsed: boolean) {
  try {
    globalThis.localStorage?.setItem(SIDEBAR_KEY, collapsed ? 'collapsed' : 'open');
  } catch {
    // Best effort.
  }
}

/** The sidebar folded to a rail: a way back, and the one action the list owned. */
function CollapsedSidebar({ onExpand }: { onExpand: () => void }) {
  const strings = useStrings();
  const { createConversation } = useConversations();
  return (
    <div
      data-slot="nannos-conversation-rail"
      className="flex shrink-0 flex-col items-center gap-1 border-r p-2"
    >
      <Button
        data-slot="nannos-conversation-expand"
        type="button"
        variant="ghost"
        size="icon-sm"
        className="size-8"
        aria-label={strings['conversations.show']}
        title={strings['conversations.show']}
        onClick={onExpand}
      >
        <PanelLeftOpenIcon />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        className="size-8"
        aria-label={strings['panel.newChat']}
        title={strings['panel.newChat']}
        onClick={() => createConversation()}
      >
        <MessageCirclePlusIcon />
      </Button>
    </div>
  );
}

function PanelContent({
  header,
  showConversationList,
}: {
  header?: ReactNode | false;
  showConversationList: boolean;
}) {
  const layout = usePanelLayout();
  const [collapsed, setCollapsed] = useState(readSidebarCollapsed);
  const setSidebar = (value: boolean) => {
    setCollapsed(value);
    writeSidebarCollapsed(value);
  };
  return (
    <div className="flex h-full min-h-0 w-full">
      {showConversationList &&
        (collapsed ? (
          <CollapsedSidebar onExpand={() => setSidebar(false)} />
        ) : (
          <ConversationList
            className={cn('shrink-0 border-r', layout === 'page' ? 'w-72' : 'w-64')}
            showNewChat
            onCollapse={() => setSidebar(true)}
          />
        ))}
      {/* One way in, never two: the header's history button appears only when
          the permanent sidebar is absent. */}
      <PanelMain header={header} hasSidebar={showConversationList} />
    </div>
  );
}

export function AssistantPanel({
  shadow = true,
  hostClassName,
  styles,
  header,
  showConversationList = false,
  className,
  customHeaders,
  playground,
  adapter,
  devMode,
  applyMode,
  layout = 'panel',
}: AssistantPanelProps) {
  const content = <PanelContent header={header} showConversationList={showConversationList} />;

  return (
    <DevModeProvider enabled={resolveDevMode(devMode)}>
      <PanelLayoutProvider layout={layout}>
        <ApplyModeProvider mode={applyMode}>
          <SendModeProvider>
            <NannosChatScope customHeaders={customHeaders} playground={playground} adapter={adapter}>
              {shadow ? (
                <ShadowPortal hostClassName={hostClassName} styles={styles} className={className}>
                  {content}
                </ShadowPortal>
              ) : (
                <div className={cn('nannos-chat flex h-full w-full flex-col', className)}>{content}</div>
              )}
            </NannosChatScope>
          </SendModeProvider>
        </ApplyModeProvider>
      </PanelLayoutProvider>
    </DevModeProvider>
  );
}
