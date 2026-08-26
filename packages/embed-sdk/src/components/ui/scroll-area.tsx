import * as React from "react"
import * as ScrollAreaPrimitive from "@radix-ui/react-scroll-area"

import { cn } from "../../lib/utils"

// forwardRef so a parent (e.g. ChatApp's auto-scroll effect) can reach the DOM
// node and query the viewport. Without this, a plain function component silently
// drops `ref` on React 18 ("Function components cannot be given refs"), leaving
// `ref.current` null — which is exactly what broke chat auto-scroll in the embed.
const ScrollArea = React.forwardRef<
  React.ElementRef<typeof ScrollAreaPrimitive.Root>,
  React.ComponentProps<typeof ScrollAreaPrimitive.Root>
>(({ className, children, ...props }, ref) => {
  return (
    <ScrollAreaPrimitive.Root
      ref={ref}
      data-slot="scroll-area"
      className={cn("relative flex flex-col", className)}
      {...props}
    >
      {/* Sized by flex, not `h-full`: a percentage height fails inside a flex
          chain constrained only by `max-height` (the history popover) — Chrome
          resolves it against the UNCLAMPED content height, so the viewport
          overflows its box and can't scroll. `flex-1` tracks the root's real
          height either way, and in an auto-height root (queue, suggestions) it
          still collapses to content. The scrollbar and corner are absolutely
          positioned, so the flex root doesn't move them.

          `[&>div]:block!` undoes the wrapper Radix puts around the children
          with an inline `display: table` (hence the `!`, which an inline style
          only loses to). A CSS table is sized by its MIN-CONTENT, and the
          min-content of a `truncate` row is the whole untruncated line — so
          rows grew WIDER than the viewport instead of truncating, pushing
          everything on their right edge (the conversation list's timestamp and
          its floating rename/delete buttons) out of sight, with no horizontal
          scrollbar to reach them. As a block it is simply the viewport's width
          and the children truncate again. Horizontal scrolling still works
          where it is wanted (`<Suggestions>`): its `w-max` row overflows the
          block and the viewport scrolls it. */}
      <ScrollAreaPrimitive.Viewport
        data-slot="scroll-area-viewport"
        className="focus-visible:ring-ring/50 min-h-0 w-full flex-1 rounded-[inherit] transition-[color,box-shadow] outline-none focus-visible:ring-[3px] focus-visible:outline-1 [&>div]:block!"
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollBar />
      <ScrollAreaPrimitive.Corner />
    </ScrollAreaPrimitive.Root>
  )
})
ScrollArea.displayName = "ScrollArea"

function ScrollBar({
  className,
  orientation = "vertical",
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>) {
  return (
    <ScrollAreaPrimitive.ScrollAreaScrollbar
      data-slot="scroll-area-scrollbar"
      orientation={orientation}
      className={cn(
        "flex touch-none p-px transition-colors select-none",
        orientation === "vertical" &&
          "h-full w-2.5 border-l border-l-transparent",
        orientation === "horizontal" &&
          "h-2.5 flex-col border-t border-t-transparent",
        className
      )}
      {...props}
    >
      <ScrollAreaPrimitive.ScrollAreaThumb
        data-slot="scroll-area-thumb"
        className="bg-border relative flex-1 rounded-full"
      />
    </ScrollAreaPrimitive.ScrollAreaScrollbar>
  )
}

export { ScrollArea, ScrollBar }
