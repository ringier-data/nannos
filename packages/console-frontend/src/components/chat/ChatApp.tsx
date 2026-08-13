import { useRef, useState } from 'react';
import { Settings, PanelRightOpen, ExternalLink, Download } from 'lucide-react';
import { cn } from '@/lib/utils';
import { config } from '@/config';
import { useAuth } from '@/contexts/AuthContext';
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
import { downloadTextFile, formatConversationAsText, slugifyFilename } from './utils';
import { useChatScroll } from './hooks/useChatScroll';

export function ChatApp() {
  const { isAdmin } = useAuth();
  const { agentInfo } = useSocket();
  const {
    messages,
    conversations,
    activeConversationId,
    liveWorkingSteps,
    isWaiting,
    hasMoreMessages,
  } = useChat();
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isTaskPanelCollapsed, setIsTaskPanelCollapsed] = useState(true);
  const scrollAreaRef = useRef<HTMLDivElement>(null);

  const agentName = agentInfo?.name || agentInfo?.title || 'A2A Assistant';

  const handleExportConversation = () => {
    const activeConversation = conversations.find((c) => c.id === activeConversationId);
    const title = activeConversation?.title || agentName;
    // Older messages the user never scrolled back to are genuinely missing from the export.
    const text = formatConversationAsText(title, messages, hasMoreMessages);
    downloadTextFile(`${slugifyFilename(title)}.txt`, text);
  };

  useChatScroll(scrollAreaRef);

  return (
    <div className="flex h-full w-full overflow-hidden bg-background text-foreground">
      {/* Left Sidebar: Conversation List */}
      <ConversationPanel />

      {/* Resize Handle (left) */}
      <div className="w-1 cursor-col-resize hover:bg-primary/30 transition-colors" aria-hidden="true" />

      {/* Center: Chat Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Chat Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-muted/30">
          <h2 className="text-sm font-semibold">{agentName}</h2>
          <div className="flex items-center gap-1">
            <ConnectionStatus />
            {activeConversationId && (
              <>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        onClick={handleExportConversation}
                        data-testid="button-export-conversation"
                        aria-label="Export conversation"
                      >
                        <Download className="h-4 w-4" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      <p>Export conversation</p>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        onClick={() => {
                          window.location.href = `/app/usage?conversation_id=${activeConversationId}`;
                        }}
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
                {isAdmin && (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => {
                            window.open(
                              `https://eu.smith.langchain.com/o/${config.langsmith.organizationId}/projects/p/${config.langsmith.projectId}/t/${activeConversationId}`,
                              '_blank',
                              'noopener,noreferrer'
                            );
                          }}
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
            {/* Show task panel button when collapsed */}
            {isTaskPanelCollapsed && (
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

        {/* HITL Interrupt Confirmation Card — shown when agent requests user approval */}
        <InterruptConfirmCard />

        {/* Chat Input */}
        <ChatInput />
      </div>

      {/* Resize Handle (right) */}
      <div
        className={cn('w-1 cursor-col-resize hover:bg-primary/30 transition-colors', isTaskPanelCollapsed && 'hidden')}
        aria-hidden="true"
      />

      {/* Right Sidebar: Task Panel */}
      <TaskPanel isCollapsed={isTaskPanelCollapsed} onToggle={() => setIsTaskPanelCollapsed(!isTaskPanelCollapsed)} />

      {/* Settings Modal */}
      <SettingsModal isOpen={isSettingsOpen} onClose={() => setIsSettingsOpen(false)} />
    </div>
  );
}
