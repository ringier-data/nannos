import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';
import { useChat } from '../contexts';

// Distance from the top of the viewport at which the next (older) page is fetched.
const LOAD_OLDER_THRESHOLD_PX = 200;

/**
 * Scroll behaviour of a chat transcript: pinned to the bottom as messages arrive,
 * and paging older history in when the reader scrolls to the top.
 *
 * A conversation opens on its newest page scrolled to the bottom, so "load more"
 * lives at the TOP of the viewport. Prepending a page makes the content above the
 * reader taller, so the position is re-anchored on a specific message element
 * (`data-message-id`, rendered by MessageCard) rather than on a scrollHeight
 * delta: the loading indicator and any streaming content below also change the
 * height, and a delta-based anchor silently spends itself on those instead.
 *
 * @param scrollAreaRef ref on the Radix ScrollArea wrapping the message list
 */
export function useChatScroll(scrollAreaRef: RefObject<HTMLDivElement | null>) {
  const { messages, isWaiting, liveWorkingSteps, activeConversationId, hasMoreMessages, isLoadingOlderMessages, loadOlderMessages } =
    useChat();

  // Set while an older page is being paged in: the message the reader was looking
  // at and where it sat on screen, so it can be put back exactly there.
  const prependAnchorRef = useRef<{ messageId: string; top: number } | null>(null);

  const getViewport = () =>
    (scrollAreaRef.current?.querySelector('[data-radix-scroll-area-viewport]') as HTMLElement | null) ?? null;

  const anchorTopmostMessage = (viewport: HTMLElement) => {
    const first = viewport.querySelector('[data-message-id]') as HTMLElement | null;
    const messageId = first?.dataset.messageId;
    if (!messageId) return false;
    prependAnchorRef.current = { messageId, top: first!.getBoundingClientRect().top };
    return true;
  };

  // Scrolling to the top pages in older history (infinite scroll upwards).
  useEffect(() => {
    const viewport = getViewport();
    if (!viewport) return;

    const requestOlderPage = () => {
      if (!hasMoreMessages || isLoadingOlderMessages || prependAnchorRef.current) return;
      if (!anchorTopmostMessage(viewport)) return;
      void loadOlderMessages();
    };

    const handleScroll = () => {
      if (viewport.scrollTop > LOAD_OLDER_THRESHOLD_PX) return;
      requestOlderPage();
    };

    // A page shorter than the viewport can't be scrolled, so no scroll event would
    // ever fire to reach the rest of the history — keep pulling until it overflows.
    if (hasMoreMessages && !isLoadingOlderMessages && viewport.scrollHeight <= viewport.clientHeight) {
      requestOlderPage();
    }

    viewport.addEventListener('scroll', handleScroll, { passive: true });
    return () => viewport.removeEventListener('scroll', handleScroll);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasMoreMessages, isLoadingOlderMessages, loadOlderMessages, messages]);

  // Drop a stale anchor when switching conversations — the next render is a fresh
  // history that must land at the bottom, not at a previous conversation's offset.
  // A layout effect declared BEFORE the scroll one so it wins the same commit.
  useLayoutEffect(() => {
    prependAnchorRef.current = null;
  }, [activeConversationId]);

  // Auto-scroll to the bottom as messages change or content streams in. A layout
  // effect so a restored scroll position is applied before the browser paints.
  useLayoutEffect(() => {
    const scrollToBottom = () => {
      const viewport = getViewport();
      if (viewport) viewport.scrollTop = viewport.scrollHeight;
    };

    // While a page of older messages is pending or has just landed, the reader is at
    // the top of the history and must stay where they are — never yanked to the bottom.
    const anchor = prependAnchorRef.current;
    const viewport = getViewport();
    if (anchor && viewport) {
      const anchored = viewport.querySelector(
        `[data-message-id="${CSS.escape(anchor.messageId)}"]`
      ) as HTMLElement | null;
      if (anchored) {
        // Put the anchored message back where it was; re-measured on every commit,
        // so the indicator appearing and disappearing can't leave the view drifted.
        viewport.scrollTop += anchored.getBoundingClientRect().top - anchor.top;
      }
      // Hold the anchor until the request settles: the page lands in one commit and
      // the indicator unmounts in another, and both move the content.
      if (!isLoadingOlderMessages) {
        prependAnchorRef.current = null;
      }
      return;
    }

    scrollToBottom();

    // Also observe DOM mutations to catch streaming updates
    const messageList = getViewport();
    if (!messageList) return;

    const observer = new MutationObserver(() => {
      // Scroll when content is added/modified
      if (isWaiting) {
        scrollToBottom();
      }
    });

    observer.observe(messageList, {
      childList: true,
      subtree: true,
      characterData: true,
      characterDataOldValue: false,
    });

    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, isWaiting, liveWorkingSteps, isLoadingOlderMessages]);
}
