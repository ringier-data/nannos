/**
 * The shell every interrupt shares — the moment the turn stops and waits for
 * the user.
 *
 * Two things stop a turn, and they are NOT the same event: a HITL guard is a
 * human decision about a proposed action (ours, `input-required` + the
 * human-in-the-loop extension), while an authorization is the task blocked on
 * the user's identity at a third party (A2A's own `auth-required` state, where
 * nothing is proposed and there is nothing to approve). They stay different on
 * the wire for that reason. What they share is entirely presentational, and it
 * lives here: one indent, one head, one type ramp, one place in the thread.
 *
 * Deliberately thin. The shell owns the frame; each card owns its own body and
 * decides what its buttons mean.
 */
import type { ReactNode } from 'react';
import { cn } from '../../lib/utils';

export interface InterruptCardProps {
  /** The head's leading glyph — amber shield for a guard, neutral for auth. */
  icon: ReactNode;
  /** What is being asked, in the interrupt's own words. One head per card. */
  title: string;
  children: ReactNode;
  className?: string;
  /** `data-slot` of the root, so hosts can style the two kinds apart. */
  slot?: string;
}

export function InterruptCard({ icon, title, children, className, slot }: InterruptCardProps) {
  return (
    <div
      data-slot={slot ?? 'nannos-interrupt-card'}
      className={cn('space-y-1.5 border-l bg-card pl-2 text-card-foreground', className)}
    >
      <div className="flex items-center gap-1.5 font-medium text-xs">
        {icon}
        {/* A head that overflows truncates rather than wrapping: it names the
            interrupt, and the body below is where the detail belongs. */}
        <span className="min-w-0 truncate font-bold">{title}</span>
      </div>
      {children}
    </div>
  );
}

/**
 * One section inside a card. A batch stacks several; a single interrupt is the
 * degenerate case of the same layout, so nothing special-cases it.
 */
export function InterruptSection({
  children,
  className,
  divided,
  slot,
}: {
  children: ReactNode;
  className?: string;
  /** Rule above — set on every section after the first. */
  divided?: boolean;
  slot?: string;
}) {
  return (
    <div
      data-slot={slot ?? 'nannos-interrupt-section'}
      className={cn('flex flex-col gap-1.5 py-2', divided && 'border-t', className)}
    >
      {children}
    </div>
  );
}

/**
 * The action row: left-aligned and compact, because this is a footnote inside a
 * turn rather than a dialog. Anything with `ml-auto` (the risk badge) is the
 * only thing that reaches the right edge.
 */
export function InterruptActions({
  children,
  className,
  slot,
}: {
  children: ReactNode;
  className?: string;
  slot?: string;
}) {
  return (
    <div
      data-slot={slot ?? 'nannos-interrupt-actions'}
      className={cn('flex items-center justify-start gap-1.5 self-stretch', className)}
    >
      {children}
    </div>
  );
}
