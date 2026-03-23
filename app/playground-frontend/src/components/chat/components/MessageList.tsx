import { AlertTriangle, Bot, User, FileText, Download } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Skeleton } from '@/components/ui/skeleton';
import { Markdown } from '@/components/ui/markdown';
import { useChat } from '../contexts';
import { formatTime } from '../utils';
import type { Message } from '../types';
import { UnifiedTimelineBlock } from './UnifiedTimelineBlock';

interface MessageCardProps {
  message: Message;
}

/**
 * MessageCard renders individual chat messages with support for file attachments.
 * 
 * File attachments include presigned S3 URLs that are hydrated by the backend
 * whenever messages are loaded, so they're always fresh.
 */

function MessageCard({ message }: MessageCardProps) {
  const isUser = message.type === 'user';
  const isError = message.content.startsWith('Error:');
  const formattedTime = formatTime(message.timestamp);

  // Extract file parts if available
  const fileParts = message.parts?.filter(part => part.kind === 'file' && part.file) || [];

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
            'rounded-lg px-4 py-2 max-w-[85%] overflow-hidden space-y-2',
            isError && 'bg-destructive/10 text-destructive border border-destructive/20',
            isUser && 'bg-primary text-primary-foreground',
            !isError && !isUser && 'bg-muted'
          )}
        >
          <Markdown inverted={isUser} className="text-sm">
            {message.content}
          </Markdown>
          
          {/* Render file attachments */}
          {fileParts.length > 0 && (
            <div className="space-y-2 mt-2">
              {fileParts.map((part, index) => {
                const file = part.file!;
                const isAudio = file.mimeType?.startsWith('audio/');
                const isImage = file.mimeType?.startsWith('image/');
                
                return (
                  <div key={index} className="border border-border/50 rounded p-2 bg-background/50">
                    {isAudio && (
                      <div className="space-y-1">
                        <p className="text-xs text-muted-foreground">{file.name || 'Audio recording'}</p>
                        <audio
                          controls
                          src={file.uri}
                          className="w-full max-w-md"
                          preload="metadata"
                        >
                          Your browser does not support the audio element.
                        </audio>
                      </div>
                    )}
                    {isImage && (
                      <div className="space-y-1">
                        <p className="text-xs text-muted-foreground">{file.name || 'Image'}</p>
                        <img
                          src={file.uri}
                          alt={file.name || 'Attachment'}
                          className="max-w-md rounded"
                        />
                      </div>
                    )}
                    {!isAudio && !isImage && (
                      <a
                        href={file.uri}
                        download={file.name}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-2 text-sm hover:underline"
                      >
                        <FileText className="w-4 h-4" />
                        <span className="flex-1 truncate">{file.name || 'Download file'}</span>
                        <Download className="w-4 h-4 shrink-0" />
                      </a>
                    )}
                  </div>
                );
              })}
            </div>
          )}
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
  const { messages, isLoadingMessages, streamingMessage, liveTimeline } = useChat();

  if (isLoadingMessages) {
    return <LoadingState />;
  }

  if (messages.length === 0 && !streamingMessage && liveTimeline.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className="flex flex-col px-4 divide-y divide-border/50">
      {messages.map((msg) => (
        <div key={msg.id}>
          {/* Render unified timeline for chronological display of all events */}
          {msg.timeline && msg.timeline.length > 0 && (
            <UnifiedTimelineBlock timeline={msg.timeline} complete={true} />
          )}
          {/* Only render MessageCard if message has actual content */}
          {msg.showMessageCard !== false && <MessageCard message={msg} />}
        </div>
      ))}
      {/* Live streaming events - unified timeline maintains chronological order */}
      {liveTimeline.length > 0 && (
        <UnifiedTimelineBlock timeline={liveTimeline} complete={false} />
      )}
      {streamingMessage && (
        <div className="flex gap-3 py-4">
          <Avatar className="shrink-0 h-8 w-8 bg-muted text-muted-foreground">
            <AvatarFallback className="bg-muted text-muted-foreground">
              <Bot className="w-4 h-4" />
            </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0 w-0 space-y-1">
            <div className="rounded-lg px-4 py-2 max-w-[85%] overflow-hidden bg-muted">
              <Markdown className="text-sm">{streamingMessage}</Markdown>
              <span className="inline-block w-1.5 h-4 bg-foreground/70 animate-pulse ml-0.5 align-text-bottom rounded-sm" />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
