import { Plus } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Skeleton } from '@/components/ui/skeleton';
import { useChat } from '../contexts';
import { formatTimestamp } from '../utils';
import type { Conversation } from '../types';

interface ConversationItemProps {
  conversation: Conversation;
  isActive: boolean;
  onClick: () => void;
}

function ConversationItem({ conversation, isActive, onClick }: ConversationItemProps) {
  const titleText = conversation.title?.trim() || 'Untitled';
  const previewText = conversation.lastMessage?.trim() || '';
  const timestampLabel = formatTimestamp(conversation.timestamp);
  const hasPreview = previewText.length > 0 && previewText !== titleText;

  return (
    <button
      className={cn(
        'w-full text-left px-3 py-3 rounded-lg border border-transparent',
        'bg-white/[0.01] text-foreground cursor-pointer',
        'transition-all duration-200',
        'hover:bg-white/[0.035] hover:border-white/5',
        'active:bg-white/5 active:border-white/[0.08]',
        'flex flex-col gap-1.5',
        isActive && 'bg-white/[0.07] border-primary/35 shadow-lg'
      )}
      onClick={onClick}
      data-testid={`conversation-${conversation.id}`}
    >
      <div className="flex items-center gap-3 min-w-0">
        <span className="font-semibold text-sm whitespace-nowrap overflow-hidden text-ellipsis flex-1 min-w-0">
          {titleText}
        </span>
        {conversation.hasActiveTasks && (
          <div
            className="w-3 h-3 border-2 border-green-400 border-t-transparent rounded-full animate-spin flex-shrink-0"
            role="status"
            aria-label="Task running"
          />
        )}
      </div>

      {timestampLabel && (
        <div className="flex items-center gap-2 flex-shrink-0">
          <time
            className="text-xs text-muted-foreground whitespace-nowrap"
            dateTime={conversation.timestamp.toISOString()}
          >
            {timestampLabel}
          </time>
        </div>
      )}

      {hasPreview && (
        <div className="text-xs text-foreground/70 whitespace-nowrap overflow-hidden text-ellipsis">{previewText}</div>
      )}
    </button>
  );
}

function LoadingState() {
  return (
    <div className="space-y-2 p-2">
      {[1, 2, 3].map((i) => (
        <div key={i} className="p-3 space-y-2">
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-3 w-1/2" />
        </div>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-8 px-4 text-center">
      <p className="text-sm font-medium text-foreground">No conversations yet</p>
      <p className="text-xs text-muted-foreground">Use the + button to create your first conversation.</p>
    </div>
  );
}

export function ConversationPanel() {
  const { conversations, activeConversationId, isLoadingConversations, createConversation, selectConversation } =
    useChat();

  return (
    <div className="w-70 min-w-50 max-w-100 flex flex-col bg-sidebar border-r border-border flex-shrink-0">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-border">
        <h2 className="text-lg font-semibold">Conversations</h2>
        <Button
          variant="ghost"
          size="icon"
          onClick={createConversation}
          data-testid="button-new-chat"
          aria-label="New conversation"
        >
          <Plus className="w-4 h-4" />
        </Button>
      </div>

      {/* Conversation List */}
      <ScrollArea className="flex-1">
        <div className="p-2 flex flex-col gap-1">
          {isLoadingConversations ? (
            <LoadingState />
          ) : conversations.length === 0 ? (
            <EmptyState />
          ) : (
            conversations.map((conv) => (
              <ConversationItem
                key={conv.id}
                conversation={conv}
                isActive={conv.id === activeConversationId}
                onClick={() => selectConversation(conv.id)}
              />
            ))
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
