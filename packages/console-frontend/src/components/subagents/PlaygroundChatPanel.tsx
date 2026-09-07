import { MessageSquare, Plus, PanelRightOpen, FlaskConical, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import {
  ApprovalCard,
  Composer,
  ConnectionStatus,
  Thread,
  WorkingBlock,
  useConversations,
  useNannosChat,
} from '@nannos/embed-sdk/panel';

interface PlaygroundChatPanelProps {
  /** Whether the conversation-list middle panel is visible */
  showConversationList: boolean;
  onToggleConversationList: () => void;
  /** Number of versions available (controls the "show version history" button) */
  versionHistoryLength: number;
  versionSidebarCollapsed: boolean;
  onShowVersionHistory: () => void;
  /** Notify the parent when the chat panel gains/loses focus (drives responsive layout) */
  onChatFocusChange: (area: 'chat' | null) => void;
  /** Formatted label for the version being tested (e.g. "v51" or "#a1b2c3d") */
  viewedVersionLabel: string;
  /** True when testing a historical (non-current) version */
  isViewingHistoricalVersion: boolean;
}

const relativeTime = (iso: string): string => {
  const diffMs = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(diffMs)) return '';
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  const minutes = Math.round(diffMs / 60_000);
  if (Math.abs(minutes) < 60) return rtf.format(minutes, 'minute');
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return rtf.format(hours, 'hour');
  return rtf.format(Math.round(hours / 24), 'day');
};

/**
 * Playground chat, recomposed from the embed-sdk v2 panel primitives (Thread /
 * Composer / ApprovalCard / WorkingBlock over `useNannosChat`). Behaves like
 * the main chat — streaming, timeline, sub-agent thoughts, HITL approvals,
 * file upload and steering — while keeping the playground's conversation list
 * and version-history layout.
 *
 * Must be rendered inside a playground-scoped `<NannosChatScope>` (see
 * SubAgentDetailPage) so it reads playground conversations and tags messages
 * with the sub-agent config hash.
 */
export function PlaygroundChatPanel({
  showConversationList,
  onToggleConversationList,
  versionHistoryLength,
  versionSidebarCollapsed,
  onShowVersionHistory,
  onChatFocusChange,
  viewedVersionLabel,
  isViewingHistoricalVersion,
}: PlaygroundChatPanelProps) {
  const { conversations, activeConversationId, createConversation, selectConversation } =
    useConversations();
  const chat = useNannosChat();

  const activeConversation = conversations.find((c) => c.id === activeConversationId);

  return (
    <>
      {/* Middle Panel - Conversation List */}
      {showConversationList && (
        <div className="w-56 flex flex-col rounded-lg border border-border bg-muted/30 overflow-hidden flex-shrink-0">
          <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
            <h3 className="text-sm font-semibold">Conversations</h3>
            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => createConversation()}>
              <Plus className="h-4 w-4" />
            </Button>
          </div>
          <ScrollArea className="flex-1 min-h-0">
            {conversations.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-3 py-12 px-4 text-center">
                <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center">
                  <MessageSquare className="w-6 h-6 text-muted-foreground" />
                </div>
                <div className="space-y-1">
                  <p className="text-sm font-medium text-foreground">No conversations</p>
                  <p className="text-xs text-muted-foreground">Click + to start a new chat</p>
                </div>
              </div>
            ) : (
              <div className="p-2 space-y-0.5">
                {conversations.map((conv) => (
                  <button
                    key={conv.id}
                    type="button"
                    className={`group w-full text-left px-3 py-2.5 rounded-md transition-colors duration-150 hover:bg-accent/50 flex items-start gap-3 ${
                      activeConversationId === conv.id
                        ? 'bg-accent text-accent-foreground'
                        : 'text-foreground/80 hover:text-foreground'
                    }`}
                    onClick={() => selectConversation(conv.id)}
                  >
                    <MessageSquare
                      className={`w-4 h-4 mt-0.5 shrink-0 ${
                        activeConversationId === conv.id ? 'text-primary' : 'text-muted-foreground'
                      }`}
                    />
                    <div className="flex-1 min-w-0 space-y-0.5">
                      <div className="flex items-center justify-between gap-2">
                        <span
                          className={`text-sm truncate ${
                            activeConversationId === conv.id ? 'font-medium' : 'font-normal'
                          }`}
                        >
                          {conv.title?.trim() || 'Untitled'}
                        </span>
                        {conv.hasActiveTasks && (
                          <Loader2 className="w-3.5 h-3.5 text-green-500 animate-spin shrink-0" />
                        )}
                      </div>
                      {relativeTime(conv.updatedAt) && (
                        <span className="text-xs text-muted-foreground block">
                          {relativeTime(conv.updatedAt)}
                        </span>
                      )}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </ScrollArea>
        </div>
      )}

      {/* Right Panel - Chat */}
      <div
        className="flex-1 flex flex-col rounded-lg border border-border bg-muted/30 min-w-0 overflow-hidden"
        onFocusCapture={() => onChatFocusChange('chat')}
        onBlurCapture={() => onChatFocusChange(null)}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
          <div className="flex items-center gap-2">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onToggleConversationList}>
                  <MessageSquare className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{showConversationList ? 'Hide conversations' : 'Show conversations'}</TooltipContent>
            </Tooltip>
            <h2 className="text-sm font-semibold">{activeConversation ? activeConversation.title : 'Test Chat'}</h2>
            <Badge variant="outline" className="text-xs">
              {viewedVersionLabel}
              {isViewingHistoricalVersion && <span className="ml-1 text-amber-600">(outdated)</span>}
            </Badge>
          </div>
          <div className="flex items-center gap-1">
            <ConnectionStatus />
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => createConversation()}>
                  <Plus className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>New conversation</TooltipContent>
            </Tooltip>
            {versionHistoryLength > 0 && versionSidebarCollapsed && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onShowVersionHistory}>
                    <PanelRightOpen className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Show version history</TooltipContent>
              </Tooltip>
            )}
          </div>
        </div>

        {/* Messages (Thread owns scrolling, streaming, empty/error states) */}
        {chat.messages.length === 0 && !chat.isBusy ? (
          <div className="flex-1 min-h-0 flex flex-col items-center justify-center py-12 text-center">
            <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center mb-3">
              <FlaskConical className="h-6 w-6 text-muted-foreground" />
            </div>
            <div className="space-y-1">
              <p className="text-sm font-medium text-foreground">Start Testing</p>
              <p className="text-xs text-muted-foreground max-w-xs">
                Send a message to test your sub-agent configuration
              </p>
            </div>
          </div>
        ) : (
          <Thread chat={chat} className="flex-1 min-h-0" />
        )}

        {/* Sticky live todos — shown while a response is in-flight */}
        {chat.isBusy && chat.workingSteps.length > 0 && (
          <div className="px-4 py-2 border-t border-border bg-muted/20 shrink-0">
            <WorkingBlock todos={chat.workingSteps} />
          </div>
        )}

        {/* HITL interrupt confirmation card */}
        {chat.interrupt.pending.length > 0 && <ApprovalCard interrupt={chat.interrupt} />}

        {/* Input (file upload, steering, read-only guard) */}
        <Composer chat={chat} />
      </div>
    </>
  );
}
