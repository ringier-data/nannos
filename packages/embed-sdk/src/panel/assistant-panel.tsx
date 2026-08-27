/**
 * `<AssistantPanel>` — the complete chat surface: engine scope, optional
 * shadow-root style isolation, optional conversation sidebar, header, thread,
 * HITL approvals, live work plan, composer and connection footer.
 */
import type { ReactNode } from 'react';
import { ActivityIcon, BugIcon, HistoryIcon, MessageCirclePlusIcon, PanelRightCloseIcon, PinIcon, PinOffIcon } from 'lucide-react';
import { Button } from '../components/ui/button';
import { Switch } from '../components/ui/switch';
import { cn } from '../lib/utils';
import { useAssistant, useStrings, type NannosHostAdapter } from '../react';
import { NannosChatScope, type PlaygroundMode } from './engine';
import { ApplyModeProvider, type ApplyMode } from './apply-mode';
import { ShadowPortal } from './shadow-portal';
import { useConversations } from './hooks/use-conversations';
import { useNannosChat } from './hooks/use-nannos-chat';
import { ApprovalCard } from './components/approval-card';
import { Composer } from './components/composer';
import { DevContextInspector } from './components/dev-context-inspector';
import { DevModeProvider, resolveDevMode, useDevMode, useDevModeControls } from './dev-mode';
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
}

/** Dev-only (English on purpose): flips the dev chrome off to preview the
 *  end-user view. Rendered only while dev mode is AVAILABLE. */
function DevModeSwitch() {
  const dev = useDevModeControls();
  if (!dev.available) return null;
  return (
    <label
      data-slot="nannos-dev-switch"
      className="flex cursor-pointer select-none items-center gap-1 rounded-md border border-amber-500/50 bg-amber-500/5 px-1.5 py-0.5"
      title={dev.active ? 'Dev view on — flip to preview the end-user view' : 'Dev view off — showing the end-user view'}
    >
      <BugIcon aria-hidden="true" className="size-3 shrink-0 text-amber-600" />
      <span className="font-medium text-amber-700 text-xs dark:text-amber-500">dev</span>
      <Switch
        checked={dev.active}
        onCheckedChange={dev.setActive}
        aria-label="Toggle developer view"
        className="h-3.5 w-6 data-[state=checked]:bg-amber-600 [&_[data-slot=switch-thumb]]:size-3"
      />
    </label>
  );
}

function PanelHeader() {
  const strings = useStrings();
  const assistant = useAssistant();
  const { conversations, activeConversationId, createConversation } = useConversations();
  const history = useConversationHistory();
  // The header names the CONVERSATION, not the agent: that is what changes as the
  // user moves through their chats, and the agent's identity is the host's own
  // chrome to show. An unnamed conversation (brand new, or waiting for the
  // backend's written title) reads as "New conversation" in the user's language.
  const active = conversations.find((c) => c.id === activeConversationId);
  const title = active?.title || strings['thread.newConversation'];

  return (
    <div data-slot="nannos-panel-header" className="flex shrink-0 items-center gap-1 border-b px-3 py-2">
      <span className="min-w-0 flex-1 truncate font-medium text-sm">{title}</span>
      <DevModeSwitch />
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
  historyAvailable,
}: {
  header?: ReactNode | false;
  historyAvailable: boolean;
}) {
  const chat = useNannosChat();
  const devMode = useDevMode();
  const strings = useStrings();

  return (
    <ConversationHistoryProvider available={historyAvailable}>
      {/* `panel.title` names the whole surface ("Assistant") — the header inside
          it names the conversation, which is what changes as the user navigates. */}
      <div
        role="region"
        aria-label={strings['panel.title']}
        className="flex min-h-0 min-w-0 flex-1 flex-col"
      >
        {header === false ? null : header === undefined ? <PanelHeader /> : header}
        {/* The history popover hangs from the top-right of THIS box, so it
            drops out of the header button without covering the thread. */}
        <div className="relative flex min-h-0 flex-1 flex-col">
          {/* `historyAvailable` is false in sidebar mode — where the list is
              already permanent, so the thread offers no second way in. */}
          <Thread chat={chat} className="min-h-0 flex-1" showContinue={historyAvailable} />
          {chat.interrupt.pending.length > 0 && (
            <ApprovalCard interrupt={chat.interrupt} className="mx-2 mb-2 shrink-0" />
          )}
          {chat.isBusy && chat.workingSteps.length > 0 && (
            <WorkingBlock todos={chat.workingSteps} className="mx-2 mb-2 shrink-0" />
          )}
          {devMode && <DevContextInspector chat={chat} className="shrink-0" />}
          <Composer chat={chat} className="shrink-0" />
          <ConversationHistoryOverlay />
        </div>
        <ConnectionStatus className="shrink-0 border-t" />
      </div>
    </ConversationHistoryProvider>
  );
}

function PanelContent({
  header,
  showConversationList,
}: {
  header?: ReactNode | false;
  showConversationList: boolean;
}) {
  return (
    <div className="flex h-full min-h-0 w-full">
      {showConversationList && <ConversationList className="w-64 shrink-0 border-r" />}
      {/* One way in, never two: the header's history button appears only when
          the permanent sidebar is absent. */}
      <PanelMain header={header} historyAvailable={!showConversationList} />
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
}: AssistantPanelProps) {
  const content = <PanelContent header={header} showConversationList={showConversationList} />;

  return (
    <DevModeProvider enabled={resolveDevMode(devMode)}>
      <ApplyModeProvider mode={applyMode}>
        <NannosChatScope customHeaders={customHeaders} playground={playground} adapter={adapter}>
          {shadow ? (
            <ShadowPortal hostClassName={hostClassName} styles={styles} className={className}>
              {content}
            </ShadowPortal>
          ) : (
            <div className={cn('nannos-chat flex h-full w-full flex-col', className)}>{content}</div>
          )}
        </NannosChatScope>
      </ApplyModeProvider>
    </DevModeProvider>
  );
}
