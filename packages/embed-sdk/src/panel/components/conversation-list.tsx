/**
 * The conversation sidebar: debounced search over `loadConversations` and the
 * list itself — title, what the conversation is about, where it started, unread
 * badge and relative time, with the active row highlighted.
 *
 * Used two ways: as the always-visible sidebar of a wide surface, and as the
 * body of the narrow panel's history overlay — which passes `onSelect` to close
 * itself once the user has picked a conversation.
 *
 * The list is the server's newest page (50 max, the endpoint's ceiling, with no
 * cursor to page past it). Search reaches older conversations by title.
 *
 * A conversation nothing has happened in yet is left out — see `isUntouched`.
 *
 * Hovering a row floats a rename and a delete button over its right edge. The
 * row reserves no space for them, so a long name has the full width until you
 * reach for them.
 *
 * Renaming is inline — the name becomes a text box in place, Enter or clicking
 * away accepts, Escape leaves it alone. The name the user types is theirs for
 * good: the backend stops writing its own title over it.
 *
 * Deletion is a SOFT delete server-side (the conversation is archived, not
 * dropped), but it is one-way from here, so it goes through a confirm dialog
 * first.
 */
import { useEffect, useRef, useState } from 'react';
import { FileTextIcon, PencilIcon, SearchIcon, Trash2Icon } from 'lucide-react';
import { Badge } from '../../components/ui/badge';
import { Button } from '../../components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog';
import { Input } from '../../components/ui/input';
import { ScrollArea } from '../../components/ui/scroll-area';
import { Spinner } from '../../components/ui/spinner';
import { cn } from '../../lib/utils';
import { format, useStrings } from '../../react';
import { useChatEngine } from '../engine';
import { useConversations } from '../hooks/use-conversations';
import { isUntouched, originLabel, previewLine, relativeTime } from '../conversation-display';
import { MAX_CONVERSATION_TITLE, type ConversationMeta } from '../../transport';

export interface ConversationListProps {
  className?: string;
  /** Called after the user picks a conversation — the history overlay closes
   *  itself on it. The sidebar passes nothing. */
  onSelect?: () => void;
  /** Put the caret in the search box on mount. The overlay does (it opened on
   *  purpose); the permanent sidebar must not steal focus from the composer. */
  autoFocusSearch?: boolean;
}

const SEARCH_DEBOUNCE_MS = 300;

export function ConversationList({
  className,
  onSelect,
  autoFocusSearch,
}: ConversationListProps) {
  const strings = useStrings();
  const {
    conversations,
    activeConversationId,
    isLoading,
    selectConversation,
    loadConversations,
    deleteConversation,
    renameConversation,
  } = useConversations();
  const { adapter } = useChatEngine();
  const visible = conversations.filter((c) => !isUntouched(c));
  const [search, setSearch] = useState('');
  // The row awaiting confirmation. Held as the whole meta, not just the id, so
  // the dialog can still name it after the row leaves the list.
  const [pendingDelete, setPendingDelete] = useState<ConversationMeta | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  // The row being renamed, and the text in its box. `null` means nobody is.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');

  // Debounced search — skipped on mount (the engine loads the list itself).
  const firstRunRef = useRef(true);
  useEffect(() => {
    if (firstRunRef.current) {
      firstRunRef.current = false;
      return;
    }
    const timer = setTimeout(() => {
      void loadConversations(search.trim() || undefined);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [search, loadConversations]);

  // Deleting the conversation the panel is READING drops it onto a fresh chat
  // (the store swaps the selection), so the overlay closes with it — leaving it
  // open over an empty thread reads as if nothing happened.
  const handleDelete = async () => {
    if (!pendingDelete) return;
    const { id } = pendingDelete;
    const wasActive = id === activeConversationId;
    setIsDeleting(true);
    const ok = await deleteConversation(id);
    setIsDeleting(false);
    setPendingDelete(null);
    if (!ok) {
      adapter.notify?.('error', strings['conversations.deleteError']);
      return;
    }
    if (wasActive) onSelect?.();
  };

  const startRename = (conversation: ConversationMeta) => {
    setEditingId(conversation.id);
    // The generated name is the starting point, not an empty box — most renames
    // are a small correction to it.
    setDraft(conversation.title);
  };

  // Committed on Enter and on losing focus (clicking away IS accepting, the way
  // a file name behaves); Escape leaves without saving. A commit that changes
  // nothing costs no request — the store returns early.
  const commitRename = async (id: string) => {
    if (editingId !== id) return; // already committed or cancelled
    const title = draft;
    setEditingId(null);
    if (!title.trim()) return; // an empty box is a cancel, not an untitling
    if (!(await renameConversation(id, title))) {
      adapter.notify?.('error', strings['conversations.renameError']);
    }
  };

  return (
    <div
      data-slot="nannos-conversation-list"
      className={cn('flex h-full min-h-0 flex-col', className)}
    >
      <div className="flex items-center gap-1.5 p-2">
        <div className="relative flex-1">
          <SearchIcon
            aria-hidden="true"
            className="pointer-events-none absolute top-2.5 left-2.5 size-3.5 text-muted-foreground"
          />
          <Input
            data-slot="nannos-conversation-search"
            type="search"
            autoFocus={autoFocusSearch}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={strings['conversations.search']}
            aria-label={strings['conversations.search']}
            className="h-8 pl-8 text-sm"
          />
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col gap-0.5 p-1.5 pt-0">
          {isLoading && visible.length === 0 && (
            <div className="flex justify-center py-4">
              <Spinner />
            </div>
          )}
          {!isLoading && visible.length === 0 && (
            <p className="px-2 py-4 text-center text-muted-foreground text-sm">
              {search.trim() ? strings['conversations.noMatches'] : strings['conversations.empty']}
            </p>
          )}
          {visible.map((conversation) => {
            const isActive = conversation.id === activeConversationId;
            const title = conversation.title || strings['thread.newConversation'];
            const origin = originLabel(conversation.origin);
            const preview = previewLine(conversation);
            // Second line: where it started, what it is about, when it last
            // moved. The origin keeps whatever it needs up to half the width;
            // the preview takes the rest and truncates. Shared with the rename
            // state, which swaps the NAME for a box and leaves this line alone
            // — the row keeps its height and its context while you type.
            const metaLine = (
              <span className="flex items-center gap-1.5 text-muted-foreground text-xs">
                {origin && (
                  <span
                    data-slot="nannos-conversation-origin"
                    className="flex max-w-[50%] shrink-0 items-center gap-1"
                    title={conversation.origin?.key}
                  >
                    <FileTextIcon aria-hidden="true" className="size-3 shrink-0" />
                    <span className="truncate">{origin}</span>
                  </span>
                )}
                {origin && preview && <span aria-hidden="true">·</span>}
                {preview && <span className="min-w-0 flex-1 truncate">{preview}</span>}
                {/* `ml-auto` keeps the time on the right edge even when there is
                    no preview to fill the space before it. */}
                <span className="ml-auto shrink-0">{relativeTime(conversation.updatedAt)}</span>
              </span>
            );

            return (
              // The action buttons cannot live INSIDE the row button — nested
              // buttons are invalid — so the row is a positioning context and
              // they float over its right edge as siblings.
              <div key={conversation.id} className="group/row relative">
                {editingId === conversation.id ? (
                  <div className="flex flex-col gap-0.5 rounded-md bg-accent px-2 py-1.5">
                    <Input
                      data-slot="nannos-conversation-rename-input"
                      autoFocus
                      value={draft}
                      maxLength={MAX_CONVERSATION_TITLE}
                      aria-label={strings['conversations.renameLabel']}
                      onChange={(event) => setDraft(event.target.value)}
                      // Clicking away accepts it, the way renaming a file does.
                      onBlur={() => void commitRename(conversation.id)}
                      onFocus={(event) => event.target.select()}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault();
                          void commitRename(conversation.id);
                        } else if (event.key === 'Escape') {
                          // The history overlay closes on Escape from a
                          // window-level listener. While a name is being edited
                          // that key belongs to the box, not to the overlay.
                          event.stopPropagation();
                          setEditingId(null);
                        }
                      }}
                      className="h-6 rounded-sm px-1.5 py-0 font-medium text-sm"
                    />
                    {metaLine}
                  </div>
                ) : (
                  <>
                    <button
                      type="button"
                      data-slot="nannos-conversation-item"
                      aria-current={isActive || undefined}
                      className={cn(
                        'cursor-pointer flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-accent hover:text-accent-foreground',
                        isActive && 'bg-accent text-accent-foreground',
                      )}
                      onClick={() => {
                        selectConversation(conversation.id);
                        onSelect?.();
                      }}
                    >
                      <span className="flex items-center gap-1.5">
                        <span
                          className={cn(
                            'min-w-0 flex-1 truncate font-medium text-sm',
                            isActive && 'font-bold',
                          )}
                        >
                          {title}
                        </span>
                        {conversation.unread > 0 && (
                          <Badge className="h-4 min-w-4 shrink-0 px-1 text-[10px]">
                            {conversation.unread}
                          </Badge>
                        )}
                      </span>
                      {metaLine}
                    </button>
                    {/* Floating over the row's right edge, so the row itself
                        reserves no space for them and a long name gets the full
                        width. Each button carries its own surface rather than
                        sharing one bar, so they read as two separate controls.
                        The surface is PINNED across hover: `ghost` would swap
                        it for `bg-accent` (translucent in dark mode), which lets
                        the row's own text show through the button. Hover is
                        signalled by the icon colour instead.
                        Faded out until the row is hovered or a button in here
                        has focus — never `hidden`, so they stay tabbable
                        and readable by screen readers. On a touch screen there
                        IS no hover, and an invisible-but-tappable button is a
                        trap, so there they stay on. */}
                    <div
                      data-slot="nannos-conversation-actions"
                      className="-translate-y-1/2 absolute top-1/2 right-1 flex items-center gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover/row:opacity-100 [@media(hover:none)]:opacity-100"
                    >
                      <Button
                        data-slot="nannos-conversation-rename"
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={format(strings['conversations.rename'], { title })}
                        title={strings['conversations.rename']}
                        className="size-7 rounded-md border bg-popover text-muted-foreground shadow-md hover:bg-popover hover:text-foreground dark:hover:bg-popover"
                        onClick={() => startRename(conversation)}
                      >
                        <PencilIcon className="size-4" />
                      </Button>
                      <Button
                        data-slot="nannos-conversation-delete"
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={format(strings['conversations.delete'], { title })}
                        title={strings['conversations.delete']}
                        className="size-7 rounded-md border bg-popover text-muted-foreground shadow-md hover:bg-popover hover:text-destructive dark:hover:bg-popover"
                        onClick={() => setPendingDelete(conversation)}
                      >
                        <Trash2Icon className="size-4" />
                      </Button>
                    </div>
                  </>
                )}
              </div>
            );
          })}
        </div>
      </ScrollArea>

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(next) => {
          if (!next && !isDeleting) setPendingDelete(null);
        }}
      >
        <DialogContent data-slot="nannos-conversation-delete-dialog">
          <DialogHeader>
            <DialogTitle>{strings['conversations.deleteTitle']}</DialogTitle>
            <DialogDescription>
              {format(strings['conversations.deleteBody'], {
                title: pendingDelete?.title || strings['thread.newConversation'],
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={isDeleting}
              onClick={() => setPendingDelete(null)}
            >
              {strings['conversations.deleteCancel']}
            </Button>
            <Button
              data-slot="nannos-conversation-delete-confirm"
              type="button"
              variant="destructive"
              disabled={isDeleting}
              onClick={() => void handleDelete()}
            >
              {isDeleting && <Spinner className="size-4" />}
              {strings['conversations.deleteConfirm']}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
