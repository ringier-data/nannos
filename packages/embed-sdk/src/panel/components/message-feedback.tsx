/**
 * Per-message thumbs up/down (ported from the v1 MessageFeedback): existing
 * ratings load ONCE per conversation via `adapter.api.getConversationFeedback`
 * (message_id → rating) and are shared through `<ConversationFeedbackProvider>`;
 * clicking submits (`submitMessageFeedback`), clicking the active rating again
 * deletes it (`deleteMessageFeedback`). State is optimistic with rollback.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type ReactNode,
} from 'react';
import { ThumbsDownIcon, ThumbsUpIcon } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '../../components/ui/tooltip';
import { cn } from '../../lib/utils';
import { useStrings, type FeedbackRating } from '../../react';
import { useChatEngine } from '../engine';

export interface ConversationFeedbackValue {
  conversationId: string;
  /** Persisted message id → current rating (optimistic). */
  ratings: Record<string, FeedbackRating>;
  /** Submit/change a rating; the SAME rating again toggles it off (delete). */
  rate: (messageId: string, rating: FeedbackRating) => Promise<void>;
}

const ConversationFeedbackContext = createContext<ConversationFeedbackValue | null>(null);

/**
 * The shared feedback map for one conversation. `enabled: false` skips the
 * fetch — used by `<MessageFeedback>` when a surrounding provider already
 * holds the map, so a thread of N messages fetches once, not N times.
 */
function useConversationFeedbackState(
  conversationId: string,
  enabled: boolean,
): ConversationFeedbackValue {
  const { adapter } = useChatEngine();
  const [ratings, setRatings] = useState<Record<string, FeedbackRating>>({});
  const ratingsRef = useRef(ratings);
  ratingsRef.current = ratings;
  const fetchedForRef = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled || fetchedForRef.current === conversationId) return;
    fetchedForRef.current = conversationId;
    setRatings({});
    let cancelled = false;
    void adapter.api
      .getConversationFeedback(conversationId)
      .then((items) => {
        if (cancelled) return;
        const fetched: Record<string, FeedbackRating> = {};
        for (const item of items) {
          if (item.message_id) fetched[item.message_id] = item.rating;
        }
        // Ratings submitted while the fetch was in flight win over the server's.
        setRatings((prev) => ({ ...fetched, ...prev }));
      })
      .catch(() => {
        // A failed fetch just starts with an empty map; rating still works.
      });
    return () => {
      cancelled = true;
    };
  }, [adapter, conversationId, enabled]);

  const rate = useCallback(
    async (messageId: string, rating: FeedbackRating) => {
      const previous = ratingsRef.current[messageId] ?? null;
      const removing = previous === rating;
      // Optimistic: reflect the click immediately, roll back on failure.
      setRatings((prev) => {
        const next = { ...prev };
        if (removing) delete next[messageId];
        else next[messageId] = rating;
        return next;
      });
      let ok = false;
      try {
        ok = removing
          ? await adapter.api.deleteMessageFeedback(conversationId, messageId)
          : await adapter.api.submitMessageFeedback(conversationId, messageId, rating);
      } catch {
        ok = false;
      }
      if (!ok) {
        setRatings((prev) => {
          const next = { ...prev };
          if (previous === null) delete next[messageId];
          else next[messageId] = previous;
          return next;
        });
      }
    },
    [adapter, conversationId],
  );

  return useMemo(() => ({ conversationId, ratings, rate }), [conversationId, ratings, rate]);
}

/** Fetches the conversation's feedback once and exposes the shared map. */
export function useConversationFeedback(conversationId: string): ConversationFeedbackValue {
  return useConversationFeedbackState(conversationId, true);
}

export interface ConversationFeedbackProviderProps {
  conversationId: string;
  children: ReactNode;
}

/** Shares one fetched feedback map with every `<MessageFeedback>` below it. */
export function ConversationFeedbackProvider({
  conversationId,
  children,
}: ConversationFeedbackProviderProps) {
  const value = useConversationFeedback(conversationId);
  return (
    <ConversationFeedbackContext.Provider value={value}>
      {children}
    </ConversationFeedbackContext.Provider>
  );
}

function FeedbackButton({
  icon: Icon,
  label,
  active,
  activeClassName,
  disabled,
  onClick,
}: {
  icon: ComponentType<{ className?: string; fill?: string }>;
  label: string;
  active: boolean;
  activeClassName: string;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={label}
          aria-pressed={active}
          disabled={disabled}
          onClick={onClick}
          className={cn(
            'size-6 rounded-sm text-muted-foreground hover:text-foreground',
            active && activeClassName,
          )}
        >
          <Icon className="size-3.5" fill={active ? 'currentColor' : 'none'} />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

export interface MessageFeedbackProps {
  conversationId: string;
  /** The PERSISTED message id (`metadata.persistedMessageId`, else the UI id). */
  messageId: string;
  className?: string;
}

/**
 * Thumbs up/down for one assistant message. Reads the shared map from a
 * surrounding `<ConversationFeedbackProvider>`; without one (a host composing
 * its own layout) it fetches the conversation's feedback itself.
 */
export function MessageFeedback({ conversationId, messageId, className }: MessageFeedbackProps) {
  const strings = useStrings();
  const shared = useContext(ConversationFeedbackContext);
  const useShared = shared !== null && shared.conversationId === conversationId;
  const own = useConversationFeedbackState(conversationId, !useShared);
  const feedback = useShared ? shared : own;

  const rating = feedback.ratings[messageId] ?? null;
  const [isPending, setIsPending] = useState(false);

  const handleRate = async (next: FeedbackRating) => {
    setIsPending(true);
    try {
      await feedback.rate(messageId, next);
    } finally {
      setIsPending(false);
    }
  };

  return (
    <div
      data-slot="nannos-message-feedback"
      data-rated={rating ?? undefined}
      className={cn('flex items-center gap-0.5', className)}
    >
      <FeedbackButton
        icon={ThumbsUpIcon}
        label={strings['feedback.helpful']}
        active={rating === 'positive'}
        activeClassName="text-green-600 hover:text-green-600 dark:text-green-400 dark:hover:text-green-400"
        disabled={isPending}
        onClick={() => void handleRate('positive')}
      />
      <FeedbackButton
        icon={ThumbsDownIcon}
        label={strings['feedback.notHelpful']}
        active={rating === 'negative'}
        activeClassName="text-red-600 hover:text-red-600 dark:text-red-400 dark:hover:text-red-400"
        disabled={isPending}
        onClick={() => void handleRate('negative')}
      />
    </div>
  );
}
