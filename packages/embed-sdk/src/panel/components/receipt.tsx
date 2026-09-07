/**
 * What a settled interrupt leaves behind.
 *
 * Both kinds of interrupt collapse, in place, into one muted line: glyph, verb
 * and subject, then qualifiers. The grammar is fixed — verb, subject,
 * dot-separated facts — so a reader scanning a long thread learns the shape
 * once and can skim decisions out of the activity stream.
 *
 * Two qualifiers are deliberate omissions rather than oversights. Risk is
 * carried only when it was High or Critical: that is the fact worth keeping
 * once the decision is made, and a "Low" badge on a settled line is noise. And
 * an authorization records that it asked the agent to retry, because otherwise
 * "Authorized GitHub" reads as the end of the story rather than the middle.
 *
 * The line is an activity-stream citizen: it folds into "Worked through N
 * steps" with the other machine lines, and the group's summary counts it so a
 * decision never disappears behind a chevron unannounced.
 */
import type { ReactNode } from 'react';
import { CheckIcon, ClockIcon, RotateCcwIcon, ShieldCheckIcon, XIcon } from 'lucide-react';
import { cn } from '../../lib/utils';
import { format, useStrings } from '../../react';
import type { NannosStrings } from '../../i18n/keys';

/** What happened to the interrupt. Not what the tool then returned. */
export type ReceiptOutcome =
  | 'approved'
  | 'rejected'
  | 'changes'
  | 'batch'
  | 'authorized'
  | 'skipped'
  | 'undecided';

const OUTCOME_ICON: Record<ReceiptOutcome, typeof CheckIcon> = {
  approved: CheckIcon,
  rejected: XIcon,
  changes: RotateCcwIcon,
  batch: CheckIcon,
  authorized: ShieldCheckIcon,
  skipped: XIcon,
  undecided: ClockIcon,
};

const OUTCOME_KEY: Record<ReceiptOutcome, keyof NannosStrings> = {
  approved: 'receipt.approved',
  rejected: 'receipt.rejected',
  changes: 'receipt.changes',
  batch: 'receipt.batch',
  authorized: 'receipt.authorized',
  skipped: 'receipt.skipped',
  undecided: 'receipt.undecided',
};

/**
 * What to say when there is no subject worth naming. Only the authorization
 * outcomes need this: an approval always has a tool title, but a credential can
 * be demanded by sandbox plumbing whose name would misinform (`eval`), and then
 * naming nothing beats naming that.
 */
const ANONYMOUS_KEY: Partial<Record<ReceiptOutcome, keyof NannosStrings>> = {
  authorized: 'receipt.authorizedAny',
  skipped: 'receipt.skippedAny',
};

/**
 * The receipt's headline. Exported because the authorization card needs the
 * same sentence for the `displayText` of the turn it sends — one phrasing, one
 * place to change it.
 */
export function receiptHeadline(
  strings: NannosStrings,
  outcome: ReceiptOutcome,
  subject: string,
): string {
  const anonymous = !subject.trim() ? ANONYMOUS_KEY[outcome] : undefined;
  if (anonymous) return strings[anonymous];
  return format(strings[OUTCOME_KEY[outcome]], { subject });
}

export interface ReceiptProps {
  outcome: ReceiptOutcome;
  /** The tool's display title, or the service that was authorized. */
  subject: string;
  /** Batch only: how many were approved out of how many. */
  counts?: { approved: number; total: number; rejected: number };
  /** Risk level string, already localized — passed only when High or Critical. */
  risk?: string;
  /** The reason the user typed, when they typed one. */
  reason?: string;
  /** Epoch ms of the decision. */
  ts?: number;
  className?: string;
}

/** `09:19` in the viewer's locale — the same clock the activity lines use. */
function clock(ts: number): string {
  try {
    return new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

/**
 * The presentational row: a glyph and one truncating line. Used by `Receipt`
 * for a decision it can describe from its parts, and directly where the text
 * has already been composed (the authorization resume, whose label is written
 * by the card that sent it).
 */
export function ReceiptLine({
  icon: Icon,
  children,
  outcome,
  className,
}: {
  icon: typeof CheckIcon;
  children: ReactNode;
  outcome: ReceiptOutcome;
  className?: string;
}) {
  return (
    <div
      data-slot="nannos-activity"
      data-receipt={outcome}
      className={cn('flex min-w-0 items-center gap-1.5 text-muted-foreground text-xs', className)}
    >
      <Icon aria-hidden="true" className="size-3 shrink-0" />
      <span className="min-w-0 truncate">{children}</span>
    </div>
  );
}

export function Receipt({
  outcome,
  subject,
  counts,
  risk,
  reason,
  ts,
  className,
}: ReceiptProps) {
  const strings = useStrings();
  const Icon = OUTCOME_ICON[outcome];
  const headline =
    outcome === 'batch' && counts
      ? format(strings['receipt.batch'], {
          approved: String(counts.approved),
          total: String(counts.total),
        })
      : receiptHeadline(strings, outcome, subject);

  // Dot-separated tail, in a fixed order: what it cost, why, when. Each piece
  // is omitted rather than blanked, so a bare decision is a bare line.
  const tail = [
    outcome === 'batch' && counts && counts.rejected > 0
      ? format(strings['receipt.batchRejected'], { rejected: String(counts.rejected) })
      : null,
    outcome === 'authorized' ? strings['receipt.retried'] : null,
    risk ? format(strings['receipt.risk'], { level: risk }) : null,
    reason?.trim() ? `“${reason.trim()}”` : null,
    ts ? clock(ts) : null,
  ].filter((piece): piece is string => Boolean(piece));

  return (
    <ReceiptLine icon={Icon} outcome={outcome} className={className}>
      <span className="font-medium">{headline}</span>
      {tail.map((piece, index) => (
        <span key={index}>
          {' · '}
          {/* The reason is the user's own words: italic, and titled so the full
              text survives the truncation the line imposes. */}
          {piece.startsWith('“') ? (
            <span className="italic" title={piece}>
              {piece}
            </span>
          ) : (
            piece
          )}
        </span>
      ))}
    </ReceiptLine>
  );
}
