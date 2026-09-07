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
 */
import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, Check, Info, Loader2, X } from 'lucide-react';

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
}: {
  /** Payload from the last real tool call, when there has been one. */
  liveResult?: Record<string, unknown>;
  /** The CEL expression, when the condition has one. */
  celExpr?: string;
  /** The judged condition, when the condition has one. */
  llmCondition?: string;
}) {
  const [source, setSource] = useState<'live' | 'mock'>('live');
  const [mockText, setMockText] = useState('');
  const [outcome, setOutcome] = useState<ValidateConditionResponse | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const cel = celExpr?.trim() || '';
  const judge = llmCondition?.trim() || '';

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
  }, [payloadKey, hasPayload, parseCheckOnly, subject, cel, judge]);

  // Derived rather than cleared in the effect: with no payload there is nothing to
  // report, and a stale outcome from a previous payload would be misleading.
  const shown = hasPayload || parseCheckOnly ? outcome : null;
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

  return (
    <div className="grid gap-2 rounded-md border border-dashed p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">
          Test this condition
        </span>
        {pending && <Loader2 className="text-muted-foreground size-3 animate-spin" />}
        <div className="ml-auto flex items-center gap-1">
          <Button
            type="button"
            size="sm"
            variant={source === 'live' ? 'secondary' : 'ghost'}
            className="h-6 px-2 text-xs"
            disabled={!liveResult}
            onClick={() => setSource('live')}
          >
            Last result
          </Button>
          <Button
            type="button"
            size="sm"
            variant={source === 'mock' ? 'secondary' : 'ghost'}
            className="h-6 px-2 text-xs"
            onClick={() => setSource('mock')}
          >
            Pasted payload
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
            className="font-mono text-xs"
            aria-invalid={Boolean(mock.error) || undefined}
          />
          {mock.error && <span className="text-destructive text-xs">{mock.error}</span>}
        </div>
      )}

      {!hasPayload && !mock.error && (
        <p className="text-muted-foreground text-xs">
          {shown?.valid === false
            ? 'The expression itself is checked without a payload; run the check or paste one to see whether it would trigger.'
            : source === 'live'
              ? 'Run the check above, or paste a payload, to see what this condition does.'
              : 'Paste a payload to see what this condition does.'}
        </p>
      )}

      {failure && (
        <span className="text-destructive flex items-start gap-1.5 text-xs">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
          {failure}
        </span>
      )}

      {shown && !shown.valid && (
        <div className="grid gap-1.5">
          <span className="text-destructive flex items-start gap-1.5 text-xs">
            <X className="mt-0.5 size-3.5 shrink-0" />
            <span>
              This expression cannot be parsed, so the job would never trigger.
              <span className="mt-0.5 block font-mono text-[11px] opacity-80">{shown.error}</span>
            </span>
          </span>
        </div>
      )}

      {/* An expression can compile yet still fail against this payload (a missing
          field, a type mismatch); on a scheduled run that fails the run, so it is an
          error here too, not a quiet "would not trigger". */}
      {shown?.valid && shown.error && (
        <div className="grid gap-1.5">
          <span className="text-destructive flex items-start gap-1.5 text-xs">
            <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
            {shown.error}
          </span>
        </div>
      )}

      {shown?.valid && !shown.error && hasPayload && (
        <div className="grid gap-1.5">
          {verdict && (
            <span
              className={cn(
                'inline-flex w-fit items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium',
                verdict.tone === 'met'
                  ? 'bg-green-600/10 text-green-700 dark:text-green-400'
                  : 'bg-muted text-muted-foreground',
              )}
            >
              {verdict.tone === 'met' && <Check className="size-3" />}
              {verdict.label}
            </span>
          )}
          <pre className="bg-muted max-h-32 overflow-auto rounded-sm px-2 py-1.5 font-mono text-[11px] leading-5">
            {JSON.stringify(shown.extracted, null, 2) ?? 'null'}
          </pre>
        </div>
      )}

      {/* The notes belong to the outcome, not to whichever branch above rendered it. */}
      {(shown?.notes ?? []).map((note) => (
        <span key={note} className="text-muted-foreground flex items-start gap-1.5 text-xs">
          <Info className="mt-0.5 size-3.5 shrink-0" />
          {note}
        </span>
      ))}
    </div>
  );
}
