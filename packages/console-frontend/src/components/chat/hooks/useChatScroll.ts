import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';
import { useChat } from '../contexts';

// Distance from the top of the viewport at which the next (older) page is fetched.
const LOAD_OLDER_THRESHOLD_PX = 200;

/**
 * Scroll behaviour of a chat transcript: pinned to the bottom as messages arrive,
 * and paging older history in when the reader scrolls to the top.
 *
 * A conversation opens on its newest page scrolled to the bottom, so "load more"
 * lives at the TOP of the viewport. Because prepending a page makes the content
 * above the reader taller, the position is re-anchored after each page lands —
 * otherwise the transcript would visibly jump under them.
 *
 * @param scrollAreaRef ref on the Radix ScrollArea wrapping the message list
 */
export function useChatScroll(scrollAreaRef: RefObject<HTMLDivElement | null>) {
  const { messages, isWaiting, liveWorkingSteps, activeConversationId, hasMoreMessages, isLoadingOlderMessages, loadOlderMessages } =
    useChat();

  // Set when an older page is requested: the scroll geometry to restore once the
  // page is prepended, so the reader stays on the message they were looking at.
  const prependAnchorRef = useRef<{ scrollHeight: number; scrollTop: number } | null>(null);

  const getViewport = () =>
    (scrollAreaRef.current?.querySelector('[data-radix-scroll-area-viewport]') as HTMLElement | null) ?? null;

  // Scrolling to the top pages in older history (infinite scroll upwards).
  useEffect(() => {
    const viewport = getViewport();
    if (!viewport) return;

    const handleScroll = () => {
      if (viewport.scrollTop > LOAD_OLDER_THRESHOLD_PX) return;
      if (!hasMoreMessages || isLoadingOlderMessages || prependAnchorRef.current) return;
      prependAnchorRef.current = { scrollHeight: viewport.scrollHeight, scrollTop: viewport.scrollTop };
      void loadOlderMessages();
    };

    viewport.addEventListener('scroll', handleScroll, { passive: true });
    return () => viewport.removeEventListener('scroll', handleScroll);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasMoreMessages, isLoadingOlderMessages, loadOlderMessages]);

  // Drop a stale anchor when switching conversations — the next render is a fresh
  // history that must land at the bottom, not at a previous conversation's offset.
  useEffect(() => {
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
      if (viewport.scrollHeight > anchor.scrollHeight) {
        // The page was prepended: offset by exactly how much taller the content got.
        prependAnchorRef.current = null;
        viewport.scrollTop = viewport.scrollHeight - anchor.scrollHeight + anchor.scrollTop;
      } else if (!isLoadingOlderMessages) {
        // The request finished without adding anything (empty page or error) —
        // release the anchor so a later scroll can try again.
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
