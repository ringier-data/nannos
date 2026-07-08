import { useRef, useState, useEffect } from 'react';
import { Settings, PanelRightOpen, ExternalLink, History, X, Plus } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useHostAdapter, useNannosCoreOptional } from '../adapter';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useSocket, useChat } from './contexts';
import {
  ConversationPanel,
  MessageList,
  ChatInput,
  ConnectionStatus,
  TaskPanel,
  SettingsModal,
} from './components';
import { WorkingBlock } from './components/WorkingBlock';
import { InterruptConfirmCard } from './components/InterruptConfirmCard';

export interface ChatAppProps {
  /** Compact single-pane layout for embedded/narrow surfaces: hides the
   *  conversation sidebar + resize handle so the chat column fills the width.
   *  The full two-pane layout (console) is the default. */
  compact?: boolean;
}

export function ChatApp({ compact = false }: ChatAppProps) {
  const { isAdmin, links, api, agentName: agentNameOverride } = useHostAdapter();
  const core = useNannosCoreOptional();
  const { agentInfo } = useSocket();
  const { messages, activeConversationId, liveWorkingSteps, isWaiting, createConversation } = useChat();
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isTaskPanelCollapsed, setIsTaskPanelCollapsed] = useState(true);
  // Compact mode has no sidebar; the header's History button swaps the chat body
  // for the (application-scoped) conversation list instead.
  const [isHistoryOpen, setIsHistoryOpen] = useState(false);
  const [subAgentName, setSubAgentName] = useState<string | null>(null);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  // Whether the view is pinned to the bottom. Starts true; flips false when the
  // user scrolls up to read history so we don't yank them back down mid-stream.
  const stickToBottomRef = useRef(true);
  const prevMessageCountRef = useRef(0);

  // Auto-resolve the scoped sub-agent's name (execute-only embeds run a sub-agent,
  // but the A2A handshake returns the orchestrator's card — "Orchestrator Agent").
  // No-op when there's no subAgentId (console) → falls back to the handshake name.
  useEffect(() => {
    let cancelled = false;
    void core?.resolveSubAgentName(api.fetch).then((n) => {
      if (!cancelled) setSubAgentName(n);
    });
    return () => {
      cancelled = true;
    };
  }, [core, api]);

  // Precedence: explicit host override → resolved sub-agent name → the A2A
  // handshake's agent name → generic fallback.
  const agentName =
    agentNameOverride || subAgentName || agentInfo?.name || agentInfo?.title || 'A2A Assistant';

  // Auto-scroll to bottom on new messages / streaming content. The shadcn
  // ScrollArea marks its scrollable viewport with data-slot="scroll-area-viewport"
  // (older Radix used data-radix-scroll-area-viewport) — match either. We scroll
  // inside requestAnimationFrame so the measurement happens after the new content
  // has been laid out (streaming chunks, cards, images), otherwise scrollHeight is
  // stale and the view lags a line behind.
  useEffect(() => {
    const viewport = scrollAreaRef.current?.querySelector<HTMLElement>(
      '[data-slot="scroll-area-viewport"],[data-radix-scroll-area-viewport]'
    );
    if (!viewport) return;

    const NEAR_BOTTOM_PX = 80;
    const isNearBottom = () =>
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < NEAR_BOTTOM_PX;
    const scrollToBottom = () => {
      requestAnimationFrame(() => {
        viewport.scrollTop = viewport.scrollHeight;
      });
    };

    // A brand-new message (user send or a fresh turn) always follows to the
    // bottom; content growth on an existing message follows only if the user is
    // still pinned near the bottom.
    const newMessageAdded = messages.length > prevMessageCountRef.current;
    prevMessageCountRef.current = messages.length;
    if (newMessageAdded) stickToBottomRef.current = true;
    if (stickToBottomRef.current) scrollToBottom();

    const onScroll = () => {
      stickToBottomRef.current = isNearBottom();
    };
    viewport.addEventListener('scroll', onScroll, { passive: true });

    // Streaming updates mutate the DOM without always changing this effect's deps;
    // follow them, but only while the user is pinned to the bottom.
    const observer = new MutationObserver(() => {
      if (stickToBottomRef.current) scrollToBottom();
    });
    observer.observe(viewport, {
      childList: true,
      subtree: true,
      characterData: true,
    });

    return () => {
      observer.disconnect();
      viewport.removeEventListener('scroll', onScroll);
    };
  }, [messages, isWaiting, liveWorkingSteps]);

  return (
    <div className="flex h-full w-full overflow-hidden bg-background text-foreground">
      {/* Left Sidebar: Conversation List (hidden in compact/embedded mode) */}
      {!compact && (
        <>
          <ConversationPanel />
          {/* Resize Handle (left) */}
          <div className="w-1 cursor-col-resize hover:bg-primary/30 transition-colors" aria-hidden="true" />
        </>
      )}

      {/* Center: Chat Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Chat Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-muted/30">
          <h2 className="text-sm font-semibold">{agentName}</h2>
          <div className="flex items-center gap-1">
            <ConnectionStatus />
            {compact && (
              <>
                {/* One-tap fresh start — resuming the last conversation is the
                    default on open, so starting a new one must not require a
                    detour through the history list. */}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => {
                    createConversation();
                    setIsHistoryOpen(false);
                  }}
                  data-testid="button-new-conversation"
                  aria-label="New conversation"
                >
                  <Plus className="h-4 w-4" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => setIsHistoryOpen((open) => !open)}
                  data-testid="button-history"
                  aria-label={isHistoryOpen ? 'Back to chat' : 'Conversation history'}
                >
                  {isHistoryOpen ? <X className="h-4 w-4" /> : <History className="h-4 w-4" />}
                </Button>
              </>
            )}
            {activeConversationId && (
              <>
                {links.usage && (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => links.usage!(activeConversationId)}
                          data-testid="button-usage-logs"
                          aria-label="View usage logs"
                        >
                          <ExternalLink className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        <p>View usage logs</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
                {isAdmin && links.trace && (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => links.trace!(activeConversationId)}
                          data-testid="button-langsmith"
                          aria-label="View trace in LangSmith"
                        >
                          <ExternalLink className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        <p>View trace in LangSmith</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
              </>
            )}
            {/* Connection/model settings are a console concern (live model catalog,
                per-session model + thinking overrides). In the compact embed the
                model is chosen server-side by the scoped sub-agent, and the picker
                would show a stale static fallback list — so hide it there. */}
            {!compact && (
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => setIsSettingsOpen(true)}
                data-testid="button-settings"
                aria-label="Settings"
              >
                <Settings className="h-4 w-4" />
              </Button>
            )}
            {/* Show task panel button when collapsed (not in compact/embedded mode) */}
            {!compact && isTaskPanelCollapsed && (
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => setIsTaskPanelCollapsed(false)}
                data-testid="button-show-tasks"
                aria-label="Show tasks"
              >
                <PanelRightOpen className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>

        {compact && isHistoryOpen ? (
          /* Compact history view: the same conversation list as the console sidebar,
             full-width. The list is server-scoped to this application's conversations
             (embedded_sub_agent_id), so no foreign titles ever render in a host page. */
          <ConversationPanel
            className="w-full flex-1 h-auto border-r-0"
            onConversationSelected={() => setIsHistoryOpen(false)}
          />
        ) : (
          <>
            {/* Chat Messages */}
            <ScrollArea className="flex-1 min-h-0" ref={scrollAreaRef}>
              <MessageList />
            </ScrollArea>

            {/* Sticky live todos — shown above the input only while a response is in-flight */}
            {liveWorkingSteps.length > 0 && (
              <div className="px-4 py-2 border-t border-border bg-muted/20">
                <WorkingBlock steps={liveWorkingSteps} complete={!isWaiting} />
              </div>
            )}

            {/* HITL Interrupt Confirmation Card — the single approval surface for all
                gated tool calls, including client-action `apply` (risk-scored by kind). */}
            <InterruptConfirmCard />

            {/* Chat Input */}
            <ChatInput />
          </>
        )}
      </div>

      {/* Right Sidebar: Task Panel (hidden entirely in compact/embedded mode) */}
      {!compact && (
        <>
          {/* Resize Handle (right) */}
          <div
            className={cn('w-1 cursor-col-resize hover:bg-primary/30 transition-colors', isTaskPanelCollapsed && 'hidden')}
            aria-hidden="true"
          />
          <TaskPanel isCollapsed={isTaskPanelCollapsed} onToggle={() => setIsTaskPanelCollapsed(!isTaskPanelCollapsed)} />
        </>
      )}

      {/* Settings Modal */}
      <SettingsModal isOpen={isSettingsOpen} onClose={() => setIsSettingsOpen(false)} />
    </div>
  );
}
