import { useState } from 'react';
import { AlertTriangle, Bot, User, FileText, Download, Flag, ThumbsUp, ThumbsDown } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { cn } from '@/lib/utils';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Skeleton } from '@/components/ui/skeleton';
import { Markdown } from '@/components/ui/markdown';
import {
  getConversationFeedbackApiV1ConversationsConversationIdFeedbackGetOptions,
  getConversationFeedbackApiV1ConversationsConversationIdFeedbackGetQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import type { FeedbackRating } from '@/api/generated';
import { useChat } from '../contexts';
import { formatTime } from '../utils';
import type { Message } from '../types';
import { UnifiedTimelineBlock } from './UnifiedTimelineBlock';
import { MessageFeedback } from './MessageFeedback';
import { ReportIssueDialog } from './ReportIssueDialog';

interface MessageCardProps {
  message: Message;
  feedbackMap?: Map<string, FeedbackRating>;
}

/**
 * MessageCard renders individual chat messages with support for file attachments.
 * 
 * File attachments include presigned S3 URLs that are hydrated by the backend
 * whenever messages are loaded, so they're always fresh.
 */

function MessageCard({ message, feedbackMap }: MessageCardProps) {
  const { activeConversationId } = useChat();
  const isUser = message.type === 'user';
  const isError = message.content.startsWith('Error:');
  const formattedTime = formatTime(message.timestamp);
  const [reportOpen, setReportOpen] = useState(false);

  // Extract file parts if available
  const fileParts = message.parts?.filter(part => part.kind === 'file' && part.file) || [];

  const currentRating = feedbackMap?.get(message.id) ?? null;

  return (
    <div
      className={cn(
        'group flex gap-3 py-4',
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
        <div className="flex items-center gap-2 px-1">
          <span className="text-xs text-muted-foreground">{formattedTime}</span>
          {!isUser && activeConversationId && (
            <div className="opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
              <MessageFeedback
                conversationId={activeConversationId}
                messageId={message.id}
                currentRating={currentRating}
              />
              <button
                type="button"
                onClick={() => setReportOpen(true)}
                className="p-1 rounded text-muted-foreground/50 hover:text-muted-foreground hover:bg-accent transition-colors"
                aria-label="Report issue"
              >
                <Flag className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
        {reportOpen && activeConversationId && (
          <ReportIssueDialog
            open={reportOpen}
            onOpenChange={setReportOpen}
            conversationId={activeConversationId}
            messageId={message.id}
          />
        )}
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

function FeedbackRequestBanner({ conversationId, subAgents, onDismiss }: { conversationId: string; subAgents: string[]; onDismiss: () => void }) {
  const queryClient = useQueryClient();
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const feedbackQueryKey = getConversationFeedbackApiV1ConversationsConversationIdFeedbackGetQueryKey({
    path: { conversation_id: conversationId },
  });

  const handleFeedback = async (rating: FeedbackRating) => {
    setSubmitting(true);
    try {
      const resp = await fetch(
        `/api/v1/conversations/${encodeURIComponent(conversationId)}/feedback`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            rating,
            ...(subAgents.length > 0 && { sub_agent_id: subAgents.join(', ') }),
          }),
        }
      );
      if (resp.ok) {
        setSubmitted(true);
        queryClient.invalidateQueries({ queryKey: feedbackQueryKey });
        setTimeout(onDismiss, 1500);
      }
    } catch {
      // Best effort
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <div className="flex items-center justify-center gap-2 py-3 px-4 bg-green-50 dark:bg-green-950/30 rounded-lg mx-4 my-2">
        <span className="text-sm text-green-700 dark:text-green-400">Thanks for your feedback!</span>
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between gap-3 py-3 px-4 bg-muted/50 border border-border rounded-lg mx-4 my-2">
      <span className="text-sm text-muted-foreground">Was this response helpful?</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => handleFeedback('positive')}
          disabled={submitting}
          className="p-1.5 rounded hover:bg-green-100 dark:hover:bg-green-900/30 text-muted-foreground hover:text-green-600 dark:hover:text-green-400 transition-colors"
          aria-label="Thumbs up"
        >
          <ThumbsUp className="w-4 h-4" />
        </button>
        <button
          type="button"
          onClick={() => handleFeedback('negative')}
          disabled={submitting}
          className="p-1.5 rounded hover:bg-red-100 dark:hover:bg-red-900/30 text-muted-foreground hover:text-red-600 dark:hover:text-red-400 transition-colors"
          aria-label="Thumbs down"
        >
          <ThumbsDown className="w-4 h-4" />
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-muted-foreground/70 hover:text-muted-foreground ml-2"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}

export function MessageList() {
  const { messages, isLoadingMessages, streamingMessage, liveTimeline, activeConversationId, pendingFeedbackRequest, dismissFeedbackRequest } = useChat();

  // Fetch feedback for the active conversation
  const { data: feedbackData } = useQuery({
    ...getConversationFeedbackApiV1ConversationsConversationIdFeedbackGetOptions({
      path: { conversation_id: activeConversationId! },
    }),
    enabled: !!activeConversationId,
  });

  // Build a map of messageId -> rating for quick lookup
  const feedbackMap = new Map<string, FeedbackRating>();
  if (feedbackData) {
    for (const fb of feedbackData) {
      feedbackMap.set(fb.message_id, fb.rating);
    }
  }

  if (isLoadingMessages) {
    return <LoadingState />;
  }

  if (messages.length === 0 && !streamingMessage && liveTimeline.length === 0) {
    return <EmptyState />;
  }

  // When the agent is actively streaming (liveTimeline exists), steering messages
  // (user messages sent while the agent is working) should render AFTER the live
  // timeline.  The first trailing user message is the one that triggered the
  // current agent turn — it stays before the timeline.  Only subsequent user
  // messages (steering) are moved after it.
  let mainMessages = messages;
  let trailingUserMessages: Message[] = [];
  if (liveTimeline.length > 0) {
    let splitIdx = messages.length;
    while (splitIdx > 0 && messages[splitIdx - 1].type === 'user') {
      splitIdx--;
    }
    // splitIdx..end are all trailing user messages.
    // Keep the first one (trigger) in mainMessages; the rest are steering.
    if (splitIdx + 1 < messages.length) {
      mainMessages = messages.slice(0, splitIdx + 1);
      trailingUserMessages = messages.slice(splitIdx + 1);
    }
  }

  return (
    <div className="flex flex-col px-4 divide-y divide-border/50">
      {mainMessages.map((msg) => (
        <div key={msg.id}>
          {/* Render unified timeline for chronological display of all events */}
          {msg.timeline && msg.timeline.length > 0 && (
            <UnifiedTimelineBlock timeline={msg.timeline} complete={true} />
          )}
          {/* Only render MessageCard if message has actual content */}
          {msg.showMessageCard !== false && <MessageCard message={msg} feedbackMap={feedbackMap} />}
        </div>
      ))}
      {/* Live streaming events - unified timeline maintains chronological order */}
      {liveTimeline.length > 0 && (
        <UnifiedTimelineBlock timeline={liveTimeline} complete={false} />
      )}
      {/* Steering messages sent while agent is streaming render after the timeline */}
      {trailingUserMessages.map((msg) => (
        <div key={msg.id}>
          <MessageCard message={msg} feedbackMap={feedbackMap} />
        </div>
      ))}
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
      {pendingFeedbackRequest?.conversationId === activeConversationId && activeConversationId && (
        <FeedbackRequestBanner
          conversationId={activeConversationId}
          subAgents={pendingFeedbackRequest.subAgents}
          onDismiss={dismissFeedbackRequest}
        />
      )}
    </div>
  );
}
