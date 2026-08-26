import * as React from "react"

import { cn } from "../../lib/utils"

// Local divergence from upstream shadcn: `pointer-fine:text-sm`, not
// `md:text-sm`. Upstream's 16px-below-md exists to stop iOS focus-zoom, but a
// viewport breakpoint is meaningless inside a docked embed (the panel's width
// doesn't track the window — shrinking the window GREW the font). Key the
// small font off a fine pointer instead; touch keeps zoom-proof 16px.
// forwardRef (not React 19 ref-as-prop): hosts may run React 18 (peer >=18),
// where a plain function component silently drops `ref`.
const Textarea = React.forwardRef<HTMLTextAreaElement, React.ComponentProps<"textarea">>(
  function Textarea({ className, ...props }, ref) {
    return (
      <textarea
        ref={ref}
        data-slot="textarea"
        className={cn(
          "border-input placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/50 aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive dark:bg-input/30 flex field-sizing-content min-h-16 w-full rounded-md border bg-transparent px-3 py-2 text-base shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50 pointer-fine:text-sm",
          className
        )}
        {...props}
      />
    )
  }
)

export { Textarea }
