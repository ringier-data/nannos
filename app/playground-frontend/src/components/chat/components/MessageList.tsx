import { AlertTriangle, Bot, Settings as SettingsIcon, User } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Card, CardContent } from '@/components/ui/card';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Skeleton } from '@/components/ui/skeleton';
import { useChat } from '../contexts';
import { convertMarkdownToHtml, formatTime } from '../utils';
import type { Message } from '../types';

interface MessageCardProps {
  message: Message;
}

function MessageCard({ message }: MessageCardProps) {
  const isUser = message.type === 'user';
  const isTask = message.type === 'task';
  const isError = message.content.startsWith('Error:');
  const formattedTime = formatTime(message.timestamp);

  const getIcon = () => {
    if (isError) return <AlertTriangle className="w-5 h-5" />;
    if (isTask) return <SettingsIcon className="w-5 h-5" />;
    if (isUser) return <User className="w-5 h-5" />;
    return <Bot className="w-5 h-5" />;
  };

  return (
    <Card
      className={cn(
        'transition-all duration-200 hover:shadow-md',
        isError && 'border-destructive/30 bg-destructive/10',
        isTask && 'border-accent bg-accent/50',
        isUser && 'border-primary/20 bg-primary/10',
        !isError && !isTask && !isUser && 'bg-card'
      )}
      data-testid={`message-${message.id}`}
      data-message-id={message.id}
    >
      <CardContent className="px-4">
        <div className="flex gap-3">
          <Avatar
            className={cn(
              'flex-shrink-0 h-10 w-10',
              isError && 'bg-destructive/20 text-destructive',
              isTask && 'bg-accent text-accent-foreground',
              isUser && 'bg-primary/20 text-primary',
              !isError && !isTask && !isUser && 'bg-secondary text-secondary-foreground'
            )}
          >
            <AvatarFallback
              className={cn(
                isError && 'bg-destructive/20 text-destructive',
                isTask && 'bg-accent text-accent-foreground',
                isUser && 'bg-primary/20 text-primary',
                !isError && !isTask && !isUser && 'bg-secondary text-secondary-foreground'
              )}
            >
              {getIcon()}
            </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0">
            <div
              className="prose prose-sm dark:prose-invert max-w-none break-words"
              dangerouslySetInnerHTML={{ __html: convertMarkdownToHtml(message.content) }}
            />
            <div className="mt-2 text-xs text-muted-foreground">{formattedTime}</div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function LoadingState() {
  return (
    <div className="space-y-4 p-4">
      {[1, 2, 3].map((i) => (
        <Card key={i}>
          <CardContent className="p-4">
            <div className="flex gap-3">
              <Skeleton className="h-10 w-10 rounded-full" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-3 w-20" />
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 px-4 text-center">
      <p className="text-sm font-medium text-foreground">Start a conversation</p>
      <p className="text-xs text-muted-foreground">Send a message below to begin chatting with the agent.</p>
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
    <div className="flex flex-col gap-4 p-4">
      {messages.map((msg) => (
        <MessageCard key={msg.id} message={msg} />
      ))}
    </div>
  );
}
