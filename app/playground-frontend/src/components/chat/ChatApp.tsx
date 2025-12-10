import { useRef, useState, useEffect } from 'react';
import { Settings, PanelRightOpen } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { useSocket, useChat } from './contexts';
import {
  ConversationPanel,
  MessageList,
  ChatInput,
  ConnectionStatus,
  TaskPanel,
  SettingsModal,
  ProfilePopover,
} from './components';

export function ChatApp() {
  const { agentInfo } = useSocket();
  const { messages } = useChat();
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isTaskPanelCollapsed, setIsTaskPanelCollapsed] = useState(true);
  const scrollAreaRef = useRef<HTMLDivElement>(null);

  const agentName = agentInfo?.name || agentInfo?.title || 'A2A Assistant';

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    if (scrollAreaRef.current) {
      // ScrollArea's viewport is the first child with data-radix-scroll-area-viewport
      const viewport = scrollAreaRef.current.querySelector('[data-radix-scroll-area-viewport]');
      if (viewport) {
        viewport.scrollTop = viewport.scrollHeight;
      }
    }
  }, [messages]);

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
            <ProfilePopover />
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
