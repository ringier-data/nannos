import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Sparkles, FileText, Download, Flag, ThumbsUp, ThumbsDown, ChevronDown, ChevronUp } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Skeleton } from '@/components/ui/skeleton';
import { Markdown } from '@/components/ui/markdown';
import { useHostAdapter, type FeedbackRating } from '../../adapter';
import { useChat } from '../contexts';
import { formatTime, getFileInfo } from '../utils';
import type { Message } from '../types';
import { UnifiedTimelineBlock } from './UnifiedTimelineBlock';
import { MessageFeedback } from './MessageFeedback';
import { ReportIssueDialog } from './ReportIssueDialog';

interface MessageCardProps {
  message: Message;
  feedbackMap?: Map<string, FeedbackRating>;
  onFeedbackChanged?: () => void;
}

/**
 * MessageCard renders individual chat messages with support for file attachments.
 *
 * Agent replies are full-width cards rather than avatar + bubble rows: the embedded
 * panel is ~400px wide and the replies are markdown documents (headings, metric
 * tables) that need every pixel. User messages stay short accent bubbles.
 *
 * File attachments include presigned S3 URLs that are hydrated by the backend
 * whenever messages are loaded, so they're always fresh.
 */

function MessageCard({ message, feedbackMap, onFeedbackChanged }: MessageCardProps) {
  const { activeConversationId } = useChat();
  const { api } = useHostAdapter();
  const isUser = message.type === 'user';
  const isError = message.content.startsWith('Error:');
  const formattedTime = formatTime(message.timestamp);
  const [reportOpen, setReportOpen] = useState(false);

  // Extract file parts if available (normalized across A2A v1.0 / legacy v0.3 shapes)
  const fileParts = (message.parts || [])
    .map(getFileInfo)
    .filter((f): f is { uri: string; mimeType?: string; name?: string } => f !== null);

  const currentRating = feedbackMap?.get(message.id) ?? null;

  return (
    <div
      // min-w-0 is load-bearing: markdown tables set a min width per column, and
      // without it a wide table pushes this flex item past the panel edge instead
      // of scrolling inside its own container.
      className={cn('group flex w-full min-w-0 flex-col', isUser && 'items-end')}
      data-testid={`message-${message.id}`}
      data-message-id={message.id}
    >
      <div
        className={cn(
          'space-y-2 overflow-hidden',
          isUser && 'max-w-[85%] rounded-nannos-card bg-nannos-accent px-3.5 py-2 text-nannos-accent-foreground',
          isError && 'w-full rounded-nannos-card border border-nannos-danger/30 bg-nannos-danger-soft px-3.5 py-3',
          !isUser && !isError && 'w-full rounded-nannos-card border border-border bg-nannos-surface-raised px-3.5 py-3',
        )}
      >
        <div className={cn(isError && 'flex gap-2')}>
          {isError && <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-nannos-danger" />}
          <Markdown inverted={isUser} className={cn('text-sm', isError && 'text-nannos-danger')}>
            {message.content}
          </Markdown>
        </div>

        {/* Render file attachments */}
        {fileParts.length > 0 && (
          <div className="space-y-2">
            {fileParts.map((file, index) => {
              const isAudio = file.mimeType?.startsWith('audio/');
              const isImage = file.mimeType?.startsWith('image/');

              return (
                <div
                  key={index}
                  className={cn(
                    'rounded-nannos-control border p-2',
                    isUser ? 'border-white/25 bg-white/10' : 'border-border bg-nannos-surface',
                  )}
                >
                  {isAudio && (
                    <div className="space-y-1">
                      <p className={cn('text-xs', isUser ? 'opacity-80' : 'text-muted-foreground')}>
                        {file.name || 'Audio recording'}
                      </p>
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
                      <p className={cn('text-xs', isUser ? 'opacity-80' : 'text-muted-foreground')}>
                        {file.name || 'Image'}
                      </p>
                      <img
                        src={file.uri}
                        alt={file.name || 'Attachment'}
                        className="max-w-full rounded-nannos-control"
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
                      <FileText className="h-4 w-4 shrink-0" />
                      <span className="flex-1 truncate">{file.name || 'Download file'}</span>
                      <Download className="h-4 w-4 shrink-0" />
                    </a>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="mt-1 flex items-center gap-2 px-1">
        <span className="text-[11px] text-muted-foreground">{formattedTime}</span>
        {!isUser && activeConversationId && (
          <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
            <MessageFeedback
              conversationId={activeConversationId}
              messageId={message.id}
              currentRating={currentRating}
              onChanged={onFeedbackChanged}
            />
            {api.reportIssue && (
              <button
                type="button"
                onClick={() => setReportOpen(true)}
                className="rounded p-1 text-muted-foreground/50 transition-colors hover:bg-accent hover:text-muted-foreground"
                aria-label="Report issue"
              >
                <Flag className="h-3.5 w-3.5" />
              </button>
            )}
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
  );
}

/**
 * A host-injected prompt (`open(prompt, { displayText })`): the page, not the
 * user, authored the request, so it must not read as user speech. Rendered as a
 * muted centered chip with the host's short label; the raw prompt that was
 * actually sent stays one tap away for transparency.
 */
function ContextChip({ message }: { message: Message }) {
  const [expanded, setExpanded] = useState(false);
  const expandable = !!message.injectedPrompt;
  return (
    <div
      className="flex flex-col items-center gap-2"
      data-testid={`message-${message.id}`}
      data-message-id={message.id}
    >
      <button
        type="button"
        onClick={() => expandable && setExpanded((v) => !v)}
        title={message.content}
        aria-expanded={expandable ? expanded : undefined}
        className={cn(
          'inline-flex max-w-[90%] items-center gap-1.5 rounded-full border border-border bg-nannos-surface-raised px-3 py-1',
          'text-xs text-muted-foreground',
          expandable && 'cursor-pointer transition-colors hover:bg-nannos-accent-subtle',
        )}
      >
        <Sparkles className="h-3 w-3 shrink-0 text-nannos-accent" />
        <span className="truncate">{message.content}</span>
        {expandable &&
          (expanded ? (
            <ChevronUp className="h-3 w-3 shrink-0" />
          ) : (
            <ChevronDown className="h-3 w-3 shrink-0" />
          ))}
      </button>
      {expanded && message.injectedPrompt && (
        <div className="max-w-[90%] rounded-nannos-control border border-border bg-nannos-surface px-3 py-2 text-xs break-words whitespace-pre-wrap text-muted-foreground">
          {message.injectedPrompt}
        </div>
      )}
    </div>
  );
}

function LoadingState() {
  return (
    <div className="space-y-3 p-4">
      {[1, 2, 3].map((i) => (
        <div key={i} className="space-y-2 rounded-nannos-card border border-border bg-nannos-surface-raised p-3">
          <Skeleton className="h-3.5 w-2/5" />
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-4/5" />
        </div>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-4 py-16 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-nannos-accent-subtle">
        <Sparkles className="h-6 w-6 text-nannos-accent" />
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">Start a conversation</p>
        <p className="text-xs text-muted-foreground">Send a message to begin chatting with the agent.</p>
      </div>
    </div>
  );
}

function FeedbackRequestBanner({
  conversationId,
  subAgents,
  onDismiss,
  onSubmitted,
}: {
  conversationId: string;
  subAgents: string[];
  onDismiss: () => void;
  onSubmitted?: () => void;
}) {
  const { api } = useHostAdapter();
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const handleFeedback = async (rating: FeedbackRating) => {
    setSubmitting(true);
    try {
      const ok = await api.submitConversationFeedback(conversationId, rating, subAgents);
      if (ok) {
        setSubmitted(true);
        onSubmitted?.();
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
      <div className="flex items-center justify-center gap-2 rounded-nannos-card bg-nannos-ok/10 px-4 py-3">
        <span className="text-sm text-nannos-ok">Thanks for your feedback!</span>
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between gap-3 rounded-nannos-card border border-border bg-nannos-surface-raised px-4 py-3">
      <span className="text-sm text-muted-foreground">Was this response helpful?</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => handleFeedback('positive')}
          disabled={submitting}
          className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-nannos-ok/10 hover:text-nannos-ok"
          aria-label="Thumbs up"
        >
          <ThumbsUp className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => handleFeedback('negative')}
          disabled={submitting}
          className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-nannos-danger/10 hover:text-nannos-danger"
          aria-label="Thumbs down"
        >
          <ThumbsDown className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className="ml-2 text-xs text-muted-foreground/70 hover:text-muted-foreground"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}

export function MessageList() {
  const { messages, isLoadingMessages, streamingMessage, liveTimeline, activeConversationId, pendingFeedbackRequest, dismissFeedbackRequest } = useChat();
  const { api } = useHostAdapter();

  // Fetch feedback for the active conversation
  const [feedbackData, setFeedbackData] = useState<Awaited<ReturnType<typeof api.getConversationFeedback>>>([]);
  const refreshFeedback = useCallback(() => {
    if (!activeConversationId) {
      setFeedbackData([]);
      return;
    }
    api
      .getConversationFeedback(activeConversationId)
      .then(setFeedbackData)
      .catch(() => setFeedbackData([]));
  }, [activeConversationId, api]);
  useEffect(() => {
    refreshFeedback();
  }, [refreshFeedback]);

  // Build a map of messageId -> rating for quick lookup
  const feedbackMap = new Map<string, FeedbackRating>();
  for (const fb of feedbackData) {
    if (fb.message_id) feedbackMap.set(fb.message_id, fb.rating);
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
    <div className="flex min-w-0 flex-col gap-3 px-4 py-3">
      {mainMessages.map((msg) => (
        <div key={msg.id} className="flex min-w-0 flex-col gap-2">
          {/* Render unified timeline for chronological display of all events */}
          {msg.timeline && msg.timeline.length > 0 && (
            <UnifiedTimelineBlock timeline={msg.timeline} complete={true} />
          )}
          {/* Only render MessageCard if message has actual content */}
          {msg.showMessageCard !== false &&
            (msg.type === 'context' ? (
              <ContextChip message={msg} />
            ) : (
              <MessageCard message={msg} feedbackMap={feedbackMap} onFeedbackChanged={refreshFeedback} />
            ))}
        </div>
      ))}
      {/* Live streaming events - unified timeline maintains chronological order */}
      {liveTimeline.length > 0 && (
        <UnifiedTimelineBlock timeline={liveTimeline} complete={false} />
      )}
      {/* Steering messages sent while agent is streaming render after the timeline */}
      {trailingUserMessages.map((msg) => (
        <MessageCard key={msg.id} message={msg} feedbackMap={feedbackMap} onFeedbackChanged={refreshFeedback} />
      ))}
      {streamingMessage && (
        <div className="w-full min-w-0 rounded-nannos-card border border-border bg-nannos-surface-raised px-3.5 py-3">
          <Markdown className="text-sm">{streamingMessage}</Markdown>
          <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse rounded-sm bg-nannos-accent align-text-bottom" />
        </div>
      )}
      {pendingFeedbackRequest?.conversationId === activeConversationId && activeConversationId && (
        <FeedbackRequestBanner
          conversationId={activeConversationId}
          subAgents={pendingFeedbackRequest.subAgents}
          onDismiss={dismissFeedbackRequest}
          onSubmitted={refreshFeedback}
        />
      )}
    </div>
  );
}
