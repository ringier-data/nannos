import { useState } from 'react';
import { ThumbsUp, ThumbsDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useHostAdapter, type FeedbackRating } from '../../adapter';

interface MessageFeedbackProps {
  conversationId: string;
  messageId: string;
  currentRating?: FeedbackRating | null;
  /** Notifies the parent so its feedback cache can refresh (replaces react-query invalidation). */
  onChanged?: () => void;
}

export function MessageFeedback({ conversationId, messageId, currentRating, onChanged }: MessageFeedbackProps) {
  const { api } = useHostAdapter();
  const [rating, setRating] = useState<FeedbackRating | null>(currentRating ?? null);
  const [isLoading, setIsLoading] = useState(false);

  const handleFeedback = async (newRating: FeedbackRating) => {
    setIsLoading(true);
    try {
      if (rating === newRating) {
        // Toggle off — remove feedback
        if (await api.deleteMessageFeedback(conversationId, messageId)) {
          setRating(null);
          onChanged?.();
        }
      } else {
        // Submit or change rating
        if (await api.submitMessageFeedback(conversationId, messageId, newRating)) {
          setRating(newRating);
          onChanged?.();
        }
      }
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        onClick={() => void handleFeedback('positive')}
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
        onClick={() => void handleFeedback('negative')}
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
