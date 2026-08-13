import { useMemo, useState } from 'react';
import { CheckCircle2, ChevronRight, Circle, Loader2 } from 'lucide-react';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';
import type { TodoItem } from '../types';

interface WorkingBlockProps {
  steps: TodoItem[];
  complete: boolean;
}

function StepIcon({ state }: { state: TodoItem['state'] }) {
  switch (state) {
    case 'completed':
      return <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-nannos-ok" />;
    case 'failed':
      return <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-nannos-danger" />;
    case 'working':
      return <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-nannos-accent" />;
    default:
      return <Circle className="h-3.5 w-3.5 shrink-0 text-muted-foreground/40" />;
  }
}

function StepItem({ step }: { step: TodoItem }) {
  return (
    <li className="flex items-center gap-1.5">
      <StepIcon state={step.state} />
      <span className={cn((step.state === 'completed' || step.state === 'failed') && 'line-through opacity-60')}>
        {step.name}
      </span>
    </li>
  );
}

function SourceGroup({ source, steps }: { source: string; steps: TodoItem[] }) {
  const finished = steps.filter((s) => s.state === 'completed' || s.state === 'failed').length;
  const allDone = finished === steps.length;

  return (
    <li className="mt-1.5 first:mt-0">
      <div className="mb-1 flex items-center gap-1.5 text-muted-foreground/70">
        {allDone ? (
          <CheckCircle2 className="h-3 w-3 shrink-0 text-nannos-ok" />
        ) : (
          <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
        )}
        <span className="font-medium">{source}</span>
        <span className="text-[10px] tabular-nums opacity-60">{finished}/{steps.length}</span>
      </div>
      <ul className="ml-4 space-y-1 border-l border-border pl-2.5">
        {steps.map((step, i) => (
          <StepItem key={i} step={step} />
        ))}
      </ul>
    </li>
  );
}

/** Counts + the step currently in flight — shared by the inline block and the strip. */
function useStepSummary(steps: TodoItem[]) {
  return useMemo(() => {
    const top: TodoItem[] = [];
    const bySource = new Map<string, TodoItem[]>();
    for (const step of steps) {
      if (step.source) {
        const arr = bySource.get(step.source);
        if (arr) arr.push(step);
        else bySource.set(step.source, [step]);
      } else {
        top.push(step);
      }
    }
    const finished = steps.filter((s) => s.state === 'completed' || s.state === 'failed').length;
    const current = steps.find((s) => s.state === 'working') ?? steps.find((s) => s.state === 'submitted');
    return { topLevel: top, grouped: bySource, finished, current };
  }, [steps]);
}

/** The step checklist itself, without any disclosure chrome around it. */
export function WorkingStepList({ steps, className }: { steps: TodoItem[]; className?: string }) {
  const { topLevel, grouped } = useStepSummary(steps);
  if (steps.length === 0) return null;

  return (
    <ul className={cn('space-y-1 text-xs text-muted-foreground', className)}>
      {topLevel.map((step, i) => (
        <StepItem key={i} step={step} />
      ))}
      {[...grouped.entries()].map(([source, sourceSteps]) => (
        <SourceGroup key={source} source={source} steps={sourceSteps} />
      ))}
    </ul>
  );
}

export function WorkingBlock({ steps, complete }: WorkingBlockProps) {
  const [open, setOpen] = useState(!complete);
  const { finished } = useStepSummary(steps);

  if (steps.length === 0) return null;

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="my-1">
      <CollapsibleTrigger className="flex cursor-pointer items-center gap-1.5 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground">
        <ChevronRight
          className={cn(
            'h-3 w-3 transition-transform duration-200',
            open && 'rotate-90'
          )}
        />
        {complete ? (
          <>
            <CheckCircle2 className="h-3 w-3 text-nannos-ok" />
            <span>Worked — {steps.length} {steps.length === 1 ? 'step' : 'steps'}</span>
          </>
        ) : (
          <>
            <Loader2 className="h-3 w-3 animate-spin" />
            <span>Working… {finished}/{steps.length}</span>
          </>
        )}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <WorkingStepList steps={steps} className="mt-1 mb-2 ml-5 border-l border-border pl-3" />
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * The live progress strip pinned above the composer while a response is in flight.
 *
 * One line by design: the name of the step running right now, which is the only
 * part a user watches. The full checklist is one click away rather than permanently
 * eating a third of a 640px-tall panel.
 */
export function WorkingStrip({ steps, complete }: WorkingBlockProps) {
  const [open, setOpen] = useState(false);
  const { finished, current } = useStepSummary(steps);

  if (steps.length === 0) return null;

  return (
    <div className="border-t border-border bg-nannos-accent-subtle px-4 py-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full cursor-pointer items-center gap-2 text-left"
        data-testid="button-working-strip"
      >
        {complete ? (
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-nannos-ok" />
        ) : (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-nannos-accent" />
        )}
        <span className="shrink-0 text-xs font-semibold text-nannos-accent">
          {complete ? 'Done' : 'Working'}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
          {current?.name ?? `${finished} of ${steps.length} steps`}
        </span>
        <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
          {finished}/{steps.length}
        </span>
        <ChevronRight
          className={cn('h-3 w-3 shrink-0 text-muted-foreground transition-transform duration-200', open && 'rotate-90')}
        />
      </button>
      {open && <WorkingStepList steps={steps} className="mt-2 border-l border-border pl-3" />}
    </div>
  );
}
