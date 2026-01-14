import { AlertTriangle, Bot, User } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Skeleton } from '@/components/ui/skeleton';
import { Markdown } from '@/components/ui/markdown';
import { useChat } from '../contexts';
import { formatTime } from '../utils';
import type { Message } from '../types';

interface MessageCardProps {
  message: Message;
}

function MessageCard({ message }: MessageCardProps) {
  const isUser = message.type === 'user';
  const isError = message.content.startsWith('Error:');
  const formattedTime = formatTime(message.timestamp);

  return (
    <div
      className={cn(
        'flex gap-3 py-4',
        isUser && 'flex-row-reverse'
      )}
      data-testid={`message-${message.id}`}
      data-message-id={message.id}
    >
      <Avatar
        className={cn(
          'shrink-0 h-8 w-8',
          isError && 'bg-destructive/20 text-destructive',
          isUser && 'bg-primary text-primary-foreground',
          !isError && !isUser && 'bg-muted text-muted-foreground'
        )}
      >
        <AvatarFallback
          className={cn(
            isError && 'bg-destructive/20 text-destructive',
            isUser && 'bg-primary text-primary-foreground',
            !isError && !isUser && 'bg-muted text-muted-foreground'
          )}
        >
          {isError ? (
            <AlertTriangle className="w-4 h-4" />
          ) : isUser ? (
            <User className="w-4 h-4" />
          ) : (
            <Bot className="w-4 h-4" />
          )}
        </AvatarFallback>
      </Avatar>
      
      <div className={cn(
        'flex-1 min-w-0 w-0 space-y-1',
        isUser && 'flex flex-col items-end'
      )}>
        <div
          className={cn(
            'rounded-lg px-4 py-2 max-w-[85%] overflow-hidden',
            isError && 'bg-destructive/10 text-destructive border border-destructive/20',
            isUser && 'bg-primary text-primary-foreground',
            !isError && !isUser && 'bg-muted'
          )}
        >
          <Markdown inverted={isUser} className="text-sm">
            {message.content}
          </Markdown>
        </div>
        <span className="text-xs text-muted-foreground px-1">{formattedTime}</span>
      </div>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="space-y-4 p-4">
      {[1, 2, 3].map((i) => (
        <div key={i} className="flex gap-3">
          <Skeleton className="h-8 w-8 rounded-full shrink-0" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-16 w-3/4 rounded-lg" />
            <Skeleton className="h-3 w-16" />
          </div>
        </div>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 px-4 text-center">
      <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center">
        <Bot className="w-6 h-6 text-muted-foreground" />
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">Start a conversation</p>
        <p className="text-xs text-muted-foreground">Send a message to begin chatting with the agent.</p>
      </div>
    </div>
  );
}

export function MessageList() {
  const { messages, isLoadingMessages } = useChat();

  if (isLoadingMessages) {
    return <LoadingState />;
  }

  if (messages.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className="flex flex-col px-4 divide-y divide-border/50">
      {messages.map((msg) => (
        <MessageCard key={msg.id} message={msg} />
      ))}
    </div>
  );
}
