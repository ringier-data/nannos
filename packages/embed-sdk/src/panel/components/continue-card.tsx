/**
 * "Continue where you left off" — the way back into the last conversation,
 * offered at the top of an EMPTY thread.
 *
 * The panel always sits on a conversation, so "no active conversation" means
 * the visible one is untouched: a fresh chat with nothing in it. In that state
 * the user's real work is one click away in the history, but nothing on screen
 * says so — the history button is a small icon, and the sidebar is absent on a
 * narrow panel. This card names the most recent conversation and reopens it.
 *
 * It shows only when there IS somewhere to go back to: an untouched thread on
 * screen and at least one touched conversation in the list.
 */
import { HistoryIcon } from 'lucide-react';
import { cn } from '../../lib/utils';
import { useStrings } from '../../react';
import { useConversations } from '../hooks/use-conversations';
import { isUntouched, originLabel, previewLine, relativeTime } from '../conversation-display';

export interface ContinueCardProps {
  className?: string;
  /** Called after the user picks the conversation — the history overlay uses
   *  the same hook to close itself; the thread passes nothing. */
  onSelect?: () => void;
}

export function ContinueCard({ className, onSelect }: ContinueCardProps) {
  const strings = useStrings();
  const { conversations, activeConversationId, selectConversation } = useConversations();

  const active = conversations.find((c) => c.id === activeConversationId);
  // Only over a thread nothing has happened in. An active conversation the user
  // is reading — even one still seeding its history, so `messages` is briefly
  // empty — is not a place to offer a way out of.
  if (active && !isUntouched(active)) return null;

  // The list arrives newest-first; the first touched row is the last one the
  // user was actually in.
  const last = conversations.find((c) => c.id !== activeConversationId && !isUntouched(c));
  if (!last) return null;

  const title = last.title || strings['thread.newConversation'];
  const origin = originLabel(last.origin);
  const preview = previewLine(last);

  return (
    <div data-slot="nannos-continue-card" className={cn('flex flex-col gap-1.5', className)}>
      <span className="px-1 font-medium text-muted-foreground text-xs">
        {strings['thread.continueTitle']}
      </span>
      <button
        type="button"
        data-slot="nannos-continue-item"
        className="cursor-pointer flex w-full items-center gap-2 rounded-lg border bg-card px-2.5 py-2 text-left transition-colors hover:bg-accent hover:text-accent-foreground"
        onClick={() => {
          selectConversation(last.id);
          onSelect?.();
        }}
      >
        <HistoryIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate font-medium text-sm">{title}</span>
          {/* Same second line as the history list: where it started, what it is
              about, when it last moved. */}
          <span className="flex items-center gap-1.5 text-muted-foreground text-xs">
            {origin && <span className="max-w-[50%] shrink-0 truncate">{origin}</span>}
            {origin && preview && <span aria-hidden="true">·</span>}
            {preview && <span className="min-w-0 flex-1 truncate">{preview}</span>}
            <span className="ml-auto shrink-0">{relativeTime(last.updatedAt)}</span>
          </span>
        </span>
      </button>
    </div>
  );
}
