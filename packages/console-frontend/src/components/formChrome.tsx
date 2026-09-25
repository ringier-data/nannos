/**
 * The small shared pieces the scheduler forms are built from.
 *
 * Extracted so the create dialog and the job detail page cannot drift in how they label
 * a section, mark a generated value or report a field error — which is exactly how they
 * drifted before.
 */
import { type ReactNode } from 'react';
import { AlertCircle, Info, Sparkles } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';

/**
 * Numbered section divider. The create-job form is a dozen fields whose meaning
 * depends on the job type; grouping them into named steps is what stops it reading
 * as one undifferentiated column.
 */
export function SectionHeader({ n, title }: { n: number; title: string }) {
  // Heavier than the field labels under it and set off by space above: a muted
  // uppercase caption read lighter than "Expression", so the steps did not split the
  // form at all.
  return (
    <div className="flex items-center gap-2.5 pt-4 first:pt-0">
      <span className="bg-primary text-primary-foreground flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold">
        {n}
      </span>
      <span className="text-foreground text-[15px] font-semibold">{title}</span>
      <span className="bg-border h-px flex-1" />
    </div>
  );
}

/**
 * The explanation a label needs once, not on every visit. Helper paragraphs under every
 * field were most of the page's height and mostly restated the label.
 *
 * A span, not a button: these sit inside fieldsets that disable their buttons, and a
 * disabled trigger never shows its tooltip.
 */
export function HintTip({ children, label = 'More about this field' }: { children: ReactNode; label?: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          aria-label={label}
          className="text-muted-foreground hover:text-foreground focus-visible:ring-ring inline-flex cursor-help rounded-full focus-visible:ring-2 focus-visible:outline-none"
        >
          <Info className="size-3.5" />
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs text-xs leading-snug font-normal">{children}</TooltipContent>
    </Tooltip>
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
  label,
}: {
  value: T;
  onChange: (next: T) => void;
  options: { value: T; label: string }[];
  className?: string;
  /** Names the choice for assistive tech; the visible label is the caller's. */
  label?: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className={cn('bg-muted flex gap-1 rounded-md p-1', className)}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
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

/**
 * Exclusive choice with room for the one line that explains the consequence.
 *
 * Drawn as a radio, not a plain card: two bordered boxes side by side read as two
 * things to fill in, and "pick one" was only discoverable by clicking.
 */
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
      role="radio"
      aria-checked={selected}
      onClick={onClick}
      className={cn(
        'bg-background flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-left shadow-xs transition-colors',
        selected ? 'border-primary ring-primary ring-1' : 'hover:border-ring',
      )}
    >
      <span
        className={cn(
          'mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border',
          selected ? 'border-primary' : 'border-muted-foreground/40',
        )}
      >
        {selected && <span className="bg-primary size-2 rounded-full" />}
      </span>
      <span className="flex flex-col gap-0.5">
        <span className="text-sm font-medium">{title}</span>
        <span className="text-muted-foreground text-xs leading-snug">{description}</span>
      </span>
    </button>
  );
}

/** Caption over an exclusive choice, so "only one of these" is said rather than implied. */
export function ChoiceLabel({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <span className="text-sm leading-none font-medium">{children}</span>
      <span className="text-muted-foreground text-xs">{hint ?? 'choose one'}</span>
    </div>
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
