/**
 * Conversation history for a NARROW panel: the same list the wide sidebar
 * renders, as a POPOVER dropping from the header's history button. It floats in
 * the top-right corner of the thread instead of covering the whole surface, so
 * the conversation stays visible behind it. Picking a conversation — or starting
 * a new one — closes it again; so does Escape, or a click outside.
 *
 * It stays INSIDE the panel's own box, which is why it is hand-positioned rather
 * than built on the Radix popover: that one portals out and does its collision
 * math against the VIEWPORT, so in a small embedded panel the list would spill
 * over the host page around it.
 *
 * Open/closed lives in a context rather than in the header, because the panel
 * header is replaceable (`<AssistantPanel header={…}>`): a host that brings its
 * own header still drives the overlay through `useConversationHistory()`.
 *
 * Not available in sidebar mode (`showConversationList`), where the same list is
 * on screen permanently — two ways in would only fight each other.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { XIcon } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { useStrings } from '../../react';
import { useConversations } from '../hooks/use-conversations';
import { ConversationList } from './conversation-list';

export interface ConversationHistoryValue {
  /** History is reachable on this surface (false in sidebar mode). */
  available: boolean;
  isOpen: boolean;
  open: () => void;
  close: () => void;
  toggle: () => void;
}

const ConversationHistoryContext = createContext<ConversationHistoryValue>({
  available: false,
  isOpen: false,
  open: () => {},
  close: () => {},
  toggle: () => {},
});

/** The history control surface — for the header button, or a host's own. */
export function useConversationHistory(): ConversationHistoryValue {
  return useContext(ConversationHistoryContext);
}

export interface ConversationHistoryProviderProps {
  /** False in sidebar mode: the list is already permanently visible. */
  available: boolean;
  children: ReactNode;
}

export function ConversationHistoryProvider({
  available,
  children,
}: ConversationHistoryProviderProps) {
  const { loadConversations } = useConversations();
  const [isOpen, setIsOpen] = useState(false);

  // Opening refetches: titles and previews move on while the panel sits on one
  // conversation (another tab, a background turn), and the engine only loads the
  // list once at mount.
  const open = useCallback(() => {
    setIsOpen(true);
    void loadConversations();
  }, [loadConversations]);
  const close = useCallback(() => setIsOpen(false), []);

  const value = useMemo<ConversationHistoryValue>(
    () => ({
      available,
      isOpen: available && isOpen,
      open,
      close,
      toggle: () => (isOpen ? close() : open()),
    }),
    [available, isOpen, open, close],
  );

  return (
    <ConversationHistoryContext.Provider value={value}>
      {children}
    </ConversationHistoryContext.Provider>
  );
}

export interface ConversationHistoryOverlayProps {
  className?: string;
}

/**
 * The control the popover hangs from — the header's history button. Found in the
 * DOM rather than through a ref, because the button is two components away and a
 * React-18 host cannot forward a ref through `<Button>`. A host that replaces
 * the header opts in by putting the same `data-slot` on its own button; without
 * it the popover just sits in the corner.
 */
const ANCHOR_SELECTOR = '[data-slot="nannos-panel-history"]';

/** Breathing room between the popover and the edges of the panel. */
const GUTTER = 4;
/** Below this the list is unreadable, so width wins over alignment. */
const MIN_WIDTH = 260;

/** Nearest history button in the same panel: search each ancestor in turn, so a
 *  second panel on the page cannot answer for this one. */
function findAnchor(from: HTMLElement | null): Element | null {
  for (let el = from; el; el = el.parentElement) {
    const hit = el.querySelector(ANCHOR_SELECTOR);
    if (hit) return hit;
  }
  return null;
}

/**
 * The popover itself. Render it as the last child of a `relative` container — it
 * hangs from that container's top-right corner, right border lined up with the
 * header's history button, and never grows past the container's edges, so the
 * thread behind it, the composer and the footer all stay on screen.
 */
export function ConversationHistoryOverlay({ className }: ConversationHistoryOverlayProps) {
  const strings = useStrings();
  const { isOpen, close } = useConversationHistory();
  const cardRef = useRef<HTMLDivElement>(null);
  // Distance from the panel's right edge, measured so the popover's right border
  // lines up with the history button's. Null until measured (and when there is
  // no button to line up with) → the plain corner gutter.
  const [rightInset, setRightInset] = useState<number | null>(null);

  // Measured, not hardcoded: what sits right of the history button depends on
  // host props (the pin button) and on button sizes. Before paint, so the
  // popover never appears in one place and jumps to another.
  useLayoutEffect(() => {
    if (!isOpen) return;
    const align = () => {
      const card = cardRef.current;
      // The positioned ancestor `right` resolves against — the panel body.
      const box = card?.offsetParent as HTMLElement | null;
      const anchor = box && findAnchor(box.parentElement);
      if (!box || !anchor) return;
      const boxRect = box.getBoundingClientRect();
      const inset = boxRect.right - anchor.getBoundingClientRect().right;
      // A panel too narrow to give the list MIN_WIDTH keeps the width and gives
      // up the alignment — a 40px-wide list would be worse than a misaligned one.
      const room = Math.max(GUTTER, boxRect.width - MIN_WIDTH);
      setRightInset(Math.min(Math.max(GUTTER, inset), room));
    };
    align();
    window.addEventListener('resize', align);
    return () => window.removeEventListener('resize', align);
  }, [isOpen]);

  // Escape closes. Bound on the window: the panel usually lives in a shadow
  // root, and keydown still reaches the window from inside one.
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isOpen, close]);

  if (!isOpen) return null;

  return (
    <>
      {/* Click anywhere else to dismiss. Transparent, so the thread stays
          readable — but it does swallow that click, which is the point: one
          click closes the list instead of also landing on what is under it. */}
      <div
        data-slot="nannos-conversation-history-backdrop"
        aria-hidden="true"
        className="absolute inset-0 z-20"
        onPointerDown={close}
      />
      <div
        ref={cardRef}
        data-slot="nannos-conversation-history"
        role="dialog"
        aria-label={strings['conversations.title']}
        style={
          rightInset === null
            ? undefined
            : { right: rightInset, maxWidth: `calc(100% - ${rightInset + GUTTER}px)` }
        }
        className={cn(
          // Bounded by the panel on both axes: it shrinks rather than hang out
          // over the host page, and the height stops short of the bottom so a
          // strip of the conversation stays visible under it.
          'absolute top-1 right-1 z-30 flex max-h-[min(34rem,calc(100%-2rem))] w-[26rem] max-w-[calc(100%-0.5rem)] min-h-0 flex-col overflow-hidden rounded-lg border bg-popover text-popover-foreground shadow-lg',
          className,
        )}
      >
        <ConversationList className="min-h-0 flex-1" onSelect={close} autoFocusSearch />
      </div>
    </>
  );
}
