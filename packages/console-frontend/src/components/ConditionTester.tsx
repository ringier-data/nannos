/**
 * Shows what a watch condition does to a payload, as it is typed.
 *
 * Two things make this necessary rather than nice. An expression can compile and
 * still fail against the real payload shape (a field the response does not have) —
 * which on a scheduled run fails the run, so it should be seen here first. And even a
 * valid expression can match nothing, which is the difference between "would not
 * trigger" and "is broken".
 *
 * The payload is either the response from "Run check now" or one pasted in, because
 * the interesting case is usually not what today's data happens to contain: you want
 * to check "an external attendee is invited" on a day when nobody external is.
 *
 * `prev` is bound the way the run binds it: to the job's stored last result. Without
 * that a change-detection condition previews as "would trigger" every time (anything
 * differs from nothing), which is the opposite of what it does on the next real run.
 */
import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, Check, ChevronDown, ClipboardPaste, Info, Loader2, X, Zap } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { type ValidateConditionResponse, validateCondition } from '@/api/scheduler';
import { cn } from '@/lib/utils';

/** Debounce before validating, so typing an expression does not fire a call per keystroke. */
const DEBOUNCE_MS = 500;

export function ConditionTester({
  liveResult,
  celExpr,
  llmCondition,
  prev,
  resultSource,
  onRun,
  running,
}: {
  /** Payload from the last real tool call, when there has been one. */
  liveResult?: Record<string, unknown>;
  /**
   * Where liveResult came from ("this check", "the last run"), shown next to the
   * verdict. A collapsed verdict without it lets a stale stored result pass for a
   * current one.
   */
  resultSource?: string;
  /** Calls the tool for a fresh response; the tester is where the answer is read. */
  onRun?: () => void;
  running?: boolean;
  /** The CEL expression, when the condition has one. */
  celExpr?: string;
  /** The judged condition, when the condition has one. */
  llmCondition?: string;
  /**
   * What the run would see as `prev`: the job's stored last result, or null when it
   * has none (a job that has not run yet). Undefined means the caller has no notion of
   * one, which is the same thing to the expression. Compared to the payload by value,
   * so a caller may pass the same object as liveResult or a fresh copy alike.
   */
  prev?: Record<string, unknown> | null;
}) {
  const [source, setSource] = useState<'live' | 'mock'>('live');
  const [mockText, setMockText] = useState('');
  const [outcome, setOutcome] = useState<ValidateConditionResponse | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  // The verdict and the count are what is read every time; the extracted items only
  // when the count looks wrong. So they are one click away rather than always open.
  const [expanded, setExpanded] = useState(false);

  const cel = celExpr?.trim() || '';
  const judge = llmCondition?.trim() || '';
  // Whether the expression reads the `prev` binding — not a string literal or a response
  // field of that name. Decides whether prev is sent at all (a stored result can be large,
  // and the backend size-checks it whether or not the expression looks at it) and whether
  // the binding is worth a note.
  const readsPrev = /(^|[^.\w])prev\b/.test(cel.replace(/'[^']*'|"[^"]*"/g, ''));
  // Serialised for the same reason as payloadKey below: a prop that changes identity
  // per render must not re-fire the debounced validate. Parsed back to a stable object
  // keyed on content, so the effect depends on the key alone.
  const prevKey = useMemo(() => (readsPrev ? JSON.stringify(prev ?? null) : ''), [readsPrev, prev]);
  const previous = useMemo<unknown>(() => (prevKey ? JSON.parse(prevKey) : null), [prevKey]);

  // Memoised rather than parsed inline: a fresh parse on every render gives `payload`
  // (and so `subject`, which the validate effect depends on) a new identity each time, so
  // every response re-triggers the effect — one backend call, an LLM call when a judge is
  // set, every debounce interval for as long as the mode stays open.
  const mock = useMemo(() => {
    if (source !== 'mock') return { value: undefined, error: null as string | null };
    if (!mockText.trim()) return { value: undefined, error: null };
    try {
      return { value: JSON.parse(mockText) as unknown, error: null };
    } catch {
      return { value: undefined, error: 'That is not valid JSON.' };
    }
  }, [source, mockText]);

  const payload = source === 'mock' ? mock.value : liveResult;
  const hasPayload = payload !== undefined;
  // A compile error does not depend on the data, so an expression can be checked
  // before there is any: validating against {} reports whether it is even legal
  // syntax, which is the failure that otherwise surfaces only when the job silently
  // never fires.
  const subject = useMemo(() => (hasPayload ? payload : {}), [hasPayload, payload]);
  const parseCheckOnly = !hasPayload && cel.length > 0;

  // Re-validate whenever the condition or the payload changes. Keyed on the
  // serialised payload so a re-run of the same tool returning the same data does not
  // trigger a pointless call.
  // Memoised: a check response can be tens of kilobytes, and this runs in the render
  // body — unmemoised it re-serialised on every keystroke anywhere in the watch form.
  const payloadKey = useMemo(
    () => (hasPayload ? JSON.stringify(payload) : ''),
    [hasPayload, payload],
  );
  useEffect(() => {
    if (!hasPayload && !parseCheckOnly) return;
    if (!cel && !judge) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      setPending(true);
      validateCondition({
        result: subject,
        prev: previous,
        cel_expr: cel || null,
        llm_condition: judge || null,
      })
        .then((res) => {
          if (cancelled) return;
          setOutcome(res);
          setFailure(null);
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          setOutcome(null);
          setFailure(e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          if (!cancelled) setPending(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // payloadKey stands in for payload; the rest are the condition's inputs.
  }, [payloadKey, hasPayload, parseCheckOnly, subject, previous, cel, judge]);
  // The verdict when nothing changed: what the run compares as prev is exactly what it
  // was given as result. By value, so a re-run that returns identical data counts too.
  const unchanged = prevKey !== '' && prevKey === payloadKey;

  // Derived rather than cleared in the effect: with no payload there is nothing to
  // report, and a stale outcome from a previous payload would be misleading.
  const shown = hasPayload || parseCheckOnly ? outcome : null;
  // Without a payload the check runs against {}, so only "does it parse" means anything:
  // every field is missing from {}, and reporting that as a failure called a correct
  // expression broken whenever no response was at hand.
  const runtimeError = hasPayload && shown?.valid ? shown.error : null;
  // A null verdict means the decision belongs to the model at run time — the gate (if
  // any) passed, and judging needs a model call this preview does not make.
  const verdict =
    shown === null || !shown.valid || shown.error || parseCheckOnly
      ? null
      : shown.condition_met !== null
        ? shown.condition_met
          ? { label: 'Would trigger', tone: 'met' as const }
          : { label: 'Would not trigger', tone: 'unmet' as const }
        : { label: 'Decided by the model at run time', tone: 'muted' as const };

  // What the condition picked out, counted: "would trigger" alone does not say whether
  // it matched the right three items or all three hundred.
  const extracted = shown?.valid && !shown.error && hasPayload ? shown.extracted : undefined;
  const matchCount = Array.isArray(extracted) ? extracted.length : null;
  const sourceText = source === 'mock' ? 'the pasted payload' : resultSource;

  return (
    <div className="bg-muted/50 grid gap-2 rounded-md px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        {pending ? (
          <Loader2 className="text-muted-foreground size-3.5 animate-spin" />
        ) : verdict ? (
          <span
            className={cn(
              'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium',
              verdict.tone === 'met'
                ? 'bg-green-600/10 text-green-700 dark:text-green-400'
                : 'bg-background text-muted-foreground',
            )}
          >
            {verdict.tone === 'met' && <Check className="size-3" />}
            {verdict.label}
          </span>
        ) : (
          <span className="text-muted-foreground text-xs">
            {shown?.valid === false
              ? 'Not tested on data yet'
              : source === 'live'
                ? 'Run the check to test this condition on a real response.'
                : 'Paste a payload to test this condition on it.'}
          </span>
        )}
        {verdict && (
          <span className="text-muted-foreground text-xs">
            {matchCount !== null && `${matchCount} ${matchCount === 1 ? 'item' : 'items'} · `}
            {sourceText ? `on ${sourceText}` : null}
          </span>
        )}

        <div className="ml-auto flex items-center gap-1">
          {extracted !== undefined && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-6 px-2 text-xs"
              aria-expanded={expanded}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? 'Hide' : 'Show'} what matched
              <ChevronDown className={cn('size-3 transition-transform', expanded && 'rotate-180')} />
            </Button>
          )}
          {onRun && source === 'live' && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-6 px-2 text-xs"
              disabled={running}
              onClick={onRun}
            >
              {running ? <Loader2 className="size-3 animate-spin" /> : <Zap className="size-3" />}
              {liveResult ? 'Run again' : 'Run check'}
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-xs"
            onClick={() => setSource(source === 'mock' ? 'live' : 'mock')}
          >
            {source === 'mock' ? (
              'Use the real response'
            ) : (
              <>
                <ClipboardPaste className="size-3" />
                Paste
              </>
            )}
          </Button>
        </div>
      </div>

      {source === 'mock' && (
        <div className="grid gap-1.5">
          <Textarea
            rows={4}
            value={mockText}
            onChange={(e) => setMockText(e.target.value)}
            placeholder={'Paste a response to test against, e.g.\n{"events": [{"attendees": [{"email": "someone@outside.com"}]}]}'}
            className="bg-background font-mono text-xs"
            aria-invalid={Boolean(mock.error) || undefined}
          />
          {mock.error && <span className="text-destructive text-xs">{mock.error}</span>}
        </div>
      )}

      {/* Errors are never behind the disclosure: they are the reason to look. */}
      {failure && (
        <span className="text-destructive flex items-start gap-1.5 text-xs">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
          {failure}
        </span>
      )}

      {shown && !shown.valid && (
        <span className="text-destructive flex items-start gap-1.5 text-xs">
          <X className="mt-0.5 size-3.5 shrink-0" />
          <span>
            This expression cannot be parsed, so the job would never trigger.
            <span className="mt-0.5 block font-mono text-[11px] opacity-80">{shown.error}</span>
          </span>
        </span>
      )}

      {/* An expression can compile yet still fail against this payload (a missing
          field, a type mismatch); on a scheduled run that fails the run, so it is an
          error here too, not a quiet "would not trigger". */}
      {runtimeError && (
        <span className="text-destructive flex items-start gap-1.5 text-xs">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
          {runtimeError}
        </span>
      )}

      {expanded && extracted !== undefined && (
        <pre className="bg-background max-h-48 overflow-auto rounded-sm px-2 py-1.5 font-mono text-[11px] leading-5">
          {JSON.stringify(extracted, null, 2) ?? 'null'}
        </pre>
      )}

      {/* Which prev the verdict was decided against. A change-detection condition reads
          the opposite way depending on it, and the binding is otherwise invisible. */}
      {shown?.valid && !shown.error && hasPayload && readsPrev && (
        <span className="text-muted-foreground flex items-start gap-1.5 text-xs">
          <Info className="mt-0.5 size-3.5 shrink-0" />
          {previous === null
            ? 'prev is null here: this job has no stored result yet, so the condition is tested as a first run.'
            : unchanged
              ? 'prev is the stored last result, the same payload as result: this is the verdict when nothing changed.'
              : 'prev is the stored last result, as on the next scheduled run.'}
        </span>
      )}

      {/* The notes belong to the outcome, not to whichever branch above rendered it. Next
          to an error they say how to fix it, so they stay out; on a good result they
          explain what was extracted, so they fold away with it. */}
      {(shown && (!shown.valid || runtimeError || (expanded && hasPayload)) ? (shown.notes ?? []) : []).map((note) => (
        <span key={note} className="text-muted-foreground flex items-start gap-1.5 text-xs">
          <Info className="mt-0.5 size-3.5 shrink-0" />
          {note}
        </span>
      ))}
    </div>
  );
}
