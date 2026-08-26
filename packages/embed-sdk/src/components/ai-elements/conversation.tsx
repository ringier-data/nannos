"use client";

import { Button } from "../ui/button";
import { cn } from "../../lib/utils";
import type { UIMessage } from "ai";
import { ArrowDownIcon, DownloadIcon } from "lucide-react";
import type { ComponentProps } from "react";
import { useCallback, useEffect } from "react";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";

export type ConversationProps = ComponentProps<typeof StickToBottom>;

/** Mirrors STICK_TO_BOTTOM_OFFSET_PX in use-stick-to-bottom. */
const ESCAPE_OFFSET_PX = 70;

/**
 * use-stick-to-bottom's own "user scrolled up" detection has two blind spots
 * in this thread: its wheel handler walks up from the event target and stops
 * at the FIRST element with `overflow: auto` — every code block has one, so
 * wheeling up over a code block never escapes the bottom-lock — and its
 * scroll handler drops events that land in a content-resize window, which
 * `content-visibility: auto` code blocks produce continuously while scrolling
 * past them. A stale lock then lets the next content growth (expanding a tool
 * call) spring the thread to the bottom under the reader. This guard listens
 * on the same scroll element and re-implements the escape without the blind
 * spots: any upward intent releases the lock unless a nested scrollable
 * element consumes the wheel.
 */
const ConversationEscapeGuard = () => {
  const { scrollRef, state, stopScroll } = useStickToBottomContext();

  useEffect(() => {
    const scrollEl = scrollRef.current;
    if (!scrollEl) return;

    const onWheel = (event: WheelEvent) => {
      if (event.deltaY >= 0 || scrollEl.scrollHeight <= scrollEl.clientHeight) return;
      if (state.animation?.ignoreEscapes) return;
      for (
        let el = event.target instanceof Element ? event.target : null;
        el && el !== scrollEl;
        el = el.parentElement
      ) {
        // A nested element consumes an upward wheel only while it can still
        // scroll up itself (a tall capped code block mid-scroll); at its top,
        // the browser chains the scroll to the thread — treat it as ours.
        if (el.scrollTop > 0 && el.scrollHeight > el.clientHeight) {
          const { overflowY } = getComputedStyle(el);
          if (overflowY === "auto" || overflowY === "scroll") return;
        }
      }
      stopScroll();
    };

    let lastScrollTop = scrollEl.scrollTop;
    const onScroll = () => {
      const { scrollTop } = scrollEl;
      // > 1px: the library's own writes leak sub-pixel "upward" moves (spring
      // rounding, its 1px overscroll correction) — only a real user move counts.
      const movedUp = lastScrollTop - scrollTop > 1;
      lastScrollTop = scrollTop;
      // Library-driven scrolls (spring animation) must not self-escape.
      if (state.animation || !state.isAtBottom || !movedUp) return;
      if (scrollEl.scrollHeight - scrollTop - scrollEl.clientHeight > ESCAPE_OFFSET_PX) {
        stopScroll();
      }
    };

    scrollEl.addEventListener("wheel", onWheel, { passive: true });
    scrollEl.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      scrollEl.removeEventListener("wheel", onWheel);
      scrollEl.removeEventListener("scroll", onScroll);
    };
  }, [scrollRef, state, stopScroll]);

  return null;
};

export const Conversation = ({ className, children, ...props }: ConversationProps) => (
  <StickToBottom
    className={cn("relative flex-1 overflow-y-hidden", className)}
    initial="smooth"
    resize="smooth"
    role="log"
    {...props}
  >
    {typeof children === "function" ? (
      children
    ) : (
      <>
        {children}
        <ConversationEscapeGuard />
      </>
    )}
  </StickToBottom>
);

export type ConversationContentProps = ComponentProps<
  typeof StickToBottom.Content
>;

export const ConversationContent = ({
  className,
  ...props
}: ConversationContentProps) => (
  <StickToBottom.Content
    className={cn("flex flex-col gap-8 p-4", className)}
    {...props}
  />
);

export type ConversationEmptyStateProps = ComponentProps<"div"> & {
  title?: string;
  description?: string;
  icon?: React.ReactNode;
};

export const ConversationEmptyState = ({
  className,
  title = "No messages yet",
  description = "Start a conversation to see messages here",
  icon,
  children,
  ...props
}: ConversationEmptyStateProps) => (
  <div
    className={cn(
      "flex size-full flex-col items-center justify-center gap-3 p-8 text-center",
      className
    )}
    {...props}
  >
    {children ?? (
      <>
        {icon && <div className="text-muted-foreground">{icon}</div>}
        <div className="space-y-1">
          <h3 className="font-medium text-sm">{title}</h3>
          {description && (
            <p className="text-muted-foreground text-sm">{description}</p>
          )}
        </div>
      </>
    )}
  </div>
);

export type ConversationScrollButtonProps = ComponentProps<typeof Button>;

export const ConversationScrollButton = ({
  className,
  ...props
}: ConversationScrollButtonProps) => {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();

  const handleScrollToBottom = useCallback(() => {
    scrollToBottom();
  }, [scrollToBottom]);

  return (
    !isAtBottom && (
      <Button
        className={cn(
          "absolute bottom-4 left-[50%] translate-x-[-50%] rounded-full dark:bg-background dark:hover:bg-muted",
          className
        )}
        onClick={handleScrollToBottom}
        size="icon"
        type="button"
        variant="outline"
        {...props}
      >
        <ArrowDownIcon className="size-4" />
      </Button>
    )
  );
};

const getMessageText = (message: UIMessage): string =>
  message.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("");

export type ConversationDownloadProps = Omit<
  ComponentProps<typeof Button>,
  "onClick"
> & {
  messages: UIMessage[];
  filename?: string;
  formatMessage?: (message: UIMessage, index: number) => string;
};

const defaultFormatMessage = (message: UIMessage): string => {
  const roleLabel =
    message.role.charAt(0).toUpperCase() + message.role.slice(1);
  return `**${roleLabel}:** ${getMessageText(message)}`;
};

export const messagesToMarkdown = (
  messages: UIMessage[],
  formatMessage: (
    message: UIMessage,
    index: number
  ) => string = defaultFormatMessage
): string => messages.map((msg, i) => formatMessage(msg, i)).join("\n\n");

export const ConversationDownload = ({
  messages,
  filename = "conversation.md",
  formatMessage = defaultFormatMessage,
  className,
  children,
  ...props
}: ConversationDownloadProps) => {
  const handleDownload = useCallback(() => {
    const markdown = messagesToMarkdown(messages, formatMessage);
    const blob = new Blob([markdown], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }, [messages, filename, formatMessage]);

  return (
    <Button
      className={cn(
        "absolute top-4 right-4 rounded-full dark:bg-background dark:hover:bg-muted",
        className
      )}
      onClick={handleDownload}
      size="icon"
      type="button"
      variant="outline"
      {...props}
    >
      {children ?? <DownloadIcon className="size-4" />}
    </Button>
  );
};
