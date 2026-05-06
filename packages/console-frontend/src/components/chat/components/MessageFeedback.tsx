import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ThumbsUp, ThumbsDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  submitFeedbackApiV1ConversationsConversationIdMessagesMessageIdFeedbackPostMutation,
  deleteFeedbackApiV1ConversationsConversationIdMessagesMessageIdFeedbackDeleteMutation,
  getConversationFeedbackApiV1ConversationsConversationIdFeedbackGetQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import type { FeedbackRating } from '@/api/generated';

interface MessageFeedbackProps {
  conversationId: string;
  messageId: string;
  currentRating?: FeedbackRating | null;
}

export function MessageFeedback({ conversationId, messageId, currentRating }: MessageFeedbackProps) {
  const queryClient = useQueryClient();
  const [rating, setRating] = useState<FeedbackRating | null>(currentRating ?? null);

  const feedbackQueryKey = getConversationFeedbackApiV1ConversationsConversationIdFeedbackGetQueryKey({
    path: { conversation_id: conversationId },
  });

  const submitMutation = useMutation({
    ...submitFeedbackApiV1ConversationsConversationIdMessagesMessageIdFeedbackPostMutation(),
    onSuccess: (_data, variables) => {
      const body = variables.body as { rating: FeedbackRating };
      setRating(body.rating);
      queryClient.invalidateQueries({ queryKey: feedbackQueryKey });
    },
  });

  const deleteMutation = useMutation({
    ...deleteFeedbackApiV1ConversationsConversationIdMessagesMessageIdFeedbackDeleteMutation(),
    onSuccess: () => {
      setRating(null);
      queryClient.invalidateQueries({ queryKey: feedbackQueryKey });
    },
  });

  const handleFeedback = (newRating: FeedbackRating) => {
    if (rating === newRating) {
      // Toggle off — remove feedback
      deleteMutation.mutate({
        path: { conversation_id: conversationId, message_id: messageId },
      });
    } else {
      // Submit or change rating
      submitMutation.mutate({
        path: { conversation_id: conversationId, message_id: messageId },
        body: { rating: newRating },
      });
    }
  };

  const isLoading = submitMutation.isPending || deleteMutation.isPending;

  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        onClick={() => handleFeedback('positive')}
        disabled={isLoading}
        className={cn(
          'p-1 rounded hover:bg-accent transition-colors',
          rating === 'positive' ? 'text-green-600 dark:text-green-400' : 'text-muted-foreground/50 hover:text-muted-foreground',
        )}
        aria-label="Thumbs up"
      >
        <ThumbsUp className="w-3.5 h-3.5" fill={rating === 'positive' ? 'currentColor' : 'none'} />
      </button>
      <button
        type="button"
        onClick={() => handleFeedback('negative')}
        disabled={isLoading}
        className={cn(
          'p-1 rounded hover:bg-accent transition-colors',
          rating === 'negative' ? 'text-red-600 dark:text-red-400' : 'text-muted-foreground/50 hover:text-muted-foreground',
        )}
        aria-label="Thumbs down"
      >
        <ThumbsDown className="w-3.5 h-3.5" fill={rating === 'negative' ? 'currentColor' : 'none'} />
      </button>
    </div>
  );
}
