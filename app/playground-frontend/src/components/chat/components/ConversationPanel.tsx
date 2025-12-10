import { Plus, MessageSquare } from 'lucide-react';
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
  const timestampLabel = formatTimestamp(conversation.timestamp);

  return (
    <button
      className={cn(
        'w-full text-left px-3 py-2.5 rounded-md',
        'transition-colors duration-150',
        'hover:bg-accent/50',
        'flex items-start gap-3',
        isActive 
          ? 'bg-accent text-accent-foreground' 
          : 'text-foreground/80 hover:text-foreground'
      )}
      onClick={onClick}
      data-testid={`conversation-${conversation.id}`}
    >
      <MessageSquare className={cn(
        'w-4 h-4 mt-0.5 shrink-0',
        isActive ? 'text-primary' : 'text-muted-foreground'
      )} />
      
      <div className="flex-1 min-w-0 space-y-0.5">
        <div className="flex items-center justify-between gap-2">
          <span className={cn(
            'text-sm truncate',
            isActive ? 'font-medium' : 'font-normal'
          )}>
            {titleText}
          </span>
          {conversation.hasActiveTasks && (
            <div
              className="w-2 h-2 bg-green-500 rounded-full animate-pulse shrink-0"
              role="status"
              aria-label="Task running"
            />
          )}
        </div>
        
        {timestampLabel && (
          <time
            className="text-xs text-muted-foreground block"
            dateTime={conversation.timestamp.toISOString()}
          >
            {timestampLabel}
          </time>
        )}
      </div>
    </button>
  );
}

function LoadingState() {
  return (
    <div className="space-y-1 p-2">
      {[1, 2, 3].map((i) => (
        <div key={i} className="flex items-start gap-3 px-3 py-2.5">
          <Skeleton className="h-4 w-4 rounded" />
          <div className="flex-1 space-y-1.5">
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-3 w-1/3" />
          </div>
        </div>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 px-4 text-center">
      <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center">
        <MessageSquare className="w-6 h-6 text-muted-foreground" />
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">No conversations</p>
        <p className="text-xs text-muted-foreground">Click + to start a new chat</p>
      </div>
    </div>
  );
}

export function ConversationPanel() {
  const { conversations, activeConversationId, isLoadingConversations, createConversation, selectConversation } =
    useChat();

  return (
    <div className="w-64 h-full flex flex-col bg-muted/30 border-r border-border flex-shrink-0 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold text-foreground">Conversations</h2>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={createConversation}
          data-testid="button-new-chat"
          aria-label="New conversation"
        >
          <Plus className="w-4 h-4" />
        </Button>
      </div>

      {/* Conversation List */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="p-2 space-y-0.5">
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
