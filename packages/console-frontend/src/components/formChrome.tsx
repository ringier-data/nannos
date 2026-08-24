/**
 * The small shared pieces the scheduler forms are built from.
 *
 * Extracted so the create dialog and the job detail page cannot drift in how they label
 * a section, mark a generated value or report a field error — which is exactly how they
 * drifted before.
 */
import { type ReactNode } from 'react';
import { AlertCircle, Sparkles } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

/**
 * Numbered section divider. The create-job form is a dozen fields whose meaning
 * depends on the job type; grouping them into named steps is what stops it reading
 * as one undifferentiated column.
 */
export function SectionHeader({ n, title }: { n: number; title: string }) {
  return (
    <div className="flex items-center gap-2.5">
      <span className="bg-secondary text-muted-foreground flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold">
        {n}
      </span>
      <span className="text-muted-foreground text-[13px] font-semibold tracking-wide uppercase">
        {title}
      </span>
      <span className="bg-border h-px flex-1" />
    </div>
  );
}

/** Marks a field the AI fill wrote, so a generated value is never taken for a typed one. */
export function AiBadge() {
  return (
    <Badge variant="secondary" className="gap-1 px-1.5 text-[10px]">
      <Sparkles className="size-2.5" /> AI
    </Badge>
  );
}

export function FieldError({ children }: { children: ReactNode }) {
  return (
    <span className="text-destructive flex items-center gap-1.5 text-xs">
      <AlertCircle className="size-3.5 shrink-0" />
      {children}
    </span>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  className,
}: {
  value: T;
  onChange: (next: T) => void;
  options: { value: T; label: string }[];
  className?: string;
}) {
  return (
    <div className={cn('bg-muted flex gap-1 rounded-md p-1', className)}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={cn(
            'h-7 flex-1 rounded-sm px-2 text-[13px] font-medium transition-colors',
            value === option.value
              ? 'bg-background text-foreground shadow-xs'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** Exclusive choice with room for the one line that explains the consequence. */
export function OptionCard({
  selected,
  title,
  description,
  onClick,
}: {
  selected: boolean;
  title: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'bg-background flex flex-col items-start gap-0.5 rounded-md border px-3 py-2.5 text-left shadow-xs transition-colors',
        selected ? 'border-primary ring-primary ring-1' : 'hover:border-ring',
      )}
    >
      <span className="text-sm font-medium">{title}</span>
      <span className="text-muted-foreground text-xs leading-snug">{description}</span>
    </button>
  );
}


/**
 * One field in read-only form: a label and the value as text.
 *
 * Not a disabled input. A greyed-out field reads as an empty placeholder, which makes the
 * value you came to look at the lowest-contrast thing on the page.
 */
export function ReadValue({
  label,
  children,
  hint,
  mono,
  empty,
}: {
  label: string;
  children?: ReactNode;
  hint?: string;
  mono?: boolean;
  /** Shown in place of the value when there is none, phrased as what happens instead. */
  empty?: string;
}) {
  const missing = children === undefined || children === null || children === '';
  return (
    <div className="grid gap-0.5">
      <span className="text-muted-foreground text-xs">{label}</span>
      <span
        className={cn(
          'text-sm leading-relaxed',
          mono && 'font-mono text-[13px]',
          missing ? 'text-muted-foreground italic' : 'text-foreground',
        )}
      >
        {missing ? (empty ?? 'Not set') : children}
      </span>
      {hint && <span className="text-muted-foreground text-xs">{hint}</span>}
    </div>
  );
}
