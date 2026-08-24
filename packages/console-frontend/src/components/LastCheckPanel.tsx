/**
 * Why the last run of a watch did what it did.
 *
 * This is the question the job detail page exists to answer, and it was unanswerable: a
 * run history full of "Condition not met" says nothing about *why* the condition was not
 * met. The material is already stored — the tool response on the job, and how the
 * condition was decided on the run — so it costs nothing but showing it.
 *
 * The two kinds of explanation are genuinely different. An expression is derivable: the
 * response is here, so it can be re-evaluated at any time. A model's judgement cannot be
 * reconstructed at all, which is why its reasoning is recorded at the run — that record
 * is the only account of it that will ever exist.
 */
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { JsonPathPicker } from '@/components/JsonPathPicker';
import type { ScheduledJobRun } from '@/api/scheduler';
import { cn } from '@/lib/utils';

function Verdict({ met }: { met: boolean | undefined }) {
  if (met === undefined) return null;
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium',
        met
          ? 'bg-green-600/10 text-green-700 dark:text-green-400'
          : 'bg-muted text-muted-foreground',
      )}
    >
      {met ? 'Triggered' : 'Did not trigger'}
    </span>
  );
}

export function LastCheckPanel({
  run,
  result,
  onUseValue,
}: {
  /** The most recent run that reached an evaluation. */
  run: ScheduledJobRun | undefined;
  /** The response that run checked, stored on the job. */
  result: Record<string, unknown> | null | undefined;
  /** Offered while editing, so a wrong path can be repointed from the real response. */
  onUseValue?: (path: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const evaluation = run?.condition_evaluation;

  // Nothing to explain: a job that has never run, or one whose runs never got as far as
  // evaluating a condition.
  if (!run || (!evaluation && !result)) return null;

  const cel = evaluation?.mode === 'cel' || evaluation?.mode === 'cel+judge';
  // A stacked evaluation has two stages, and the record must show which one made the
  // call: a false gate means the model was never asked at all.
  const celJudged = evaluation?.mode === 'cel+judge' && evaluation.gate_met === true;

  const badge = cel
    ? celJudged
      ? 'expression gate, then judged by a model'
      : 'decided by an expression'
    : 'judged by a model';

  return (
    <div className="overflow-hidden rounded-lg border">
      <div className="bg-muted flex items-center justify-between gap-3 border-b px-4 py-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-[15px] font-semibold">Last check</span>
          <span className="text-muted-foreground text-xs">
            {new Date(run.started_at).toLocaleString()}
          </span>
          <Verdict met={evaluation?.met} />
          {evaluation && (
            <Badge variant="outline" className="text-[10px]">
              {badge}
            </Badge>
          )}
        </div>
        <Button type="button" variant="outline" size="sm" onClick={() => setOpen(!open)}>
          {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          {open ? 'Hide' : 'Show'}
        </Button>
      </div>

      {open && (
        <div className="grid lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="lg:border-r">
            <div className="flex items-center justify-between gap-2 border-b px-3.5 py-1.5">
              <span className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">
                Response
              </span>
              {onUseValue && (
                <span className="text-muted-foreground text-[11px]">
                  click a value to watch it instead
                </span>
              )}
            </div>
            {result ? (
              <JsonPathPicker
                value={result}
                allowObjects
                onPick={(path) => onUseValue?.(path)}
              />
            ) : (
              <p className="text-muted-foreground px-3.5 py-3 text-xs">
                The response was not stored for this run.
              </p>
            )}
          </div>

          <div className="grid content-start gap-3 p-3.5">
            <span className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">
              Why
            </span>

            {cel ? (
              <div className="grid gap-1.5">
                <p className="text-[13px] leading-relaxed">
                  {evaluation?.gate_met
                    ? 'The expression matched — its result is the evidence below.'
                    : 'The expression matched nothing, so there was nothing to trigger on.'}
                  {celJudged &&
                    (evaluation?.reasoning
                      ? ` The model then judged it: “${evaluation.reasoning}”`
                      : ' The model then judged it, but gave no reason.')}
                  {evaluation?.mode === 'cel+judge' &&
                    evaluation.gate_met === false &&
                    ' The model was never asked — the gate decides that for free.'}
                </p>
                {celJudged && (
                  <span className="text-muted-foreground text-xs">
                    The model’s account, recorded when the run happened. It cannot be
                    recomputed, so this is the only explanation this run will ever have.
                  </span>
                )}
              </div>
            ) : (
              <div className="grid gap-1.5">
                <p className="text-[13px] leading-relaxed">
                  {evaluation?.reasoning ? `“${evaluation.reasoning}”` : 'The model gave no reason.'}
                </p>
                <span className="text-muted-foreground text-xs">
                  The model’s own account, recorded when the run happened. It cannot be
                  recomputed, so this is the only explanation this run will ever have.
                </span>
              </div>
            )}

            {evaluation && (
              <div className="grid gap-1 border-t pt-2.5">
                <span className="text-muted-foreground text-[11px]">Value it looked at</span>
                <span className="font-mono text-xs break-words">
                  {evaluation.extracted === null || evaluation.extracted === undefined
                    ? '(nothing)'
                    : typeof evaluation.extracted === 'string'
                      ? evaluation.extracted
                      : JSON.stringify(evaluation.extracted)}
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
