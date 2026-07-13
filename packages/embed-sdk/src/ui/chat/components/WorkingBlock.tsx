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
      return <CheckCircle2 className="w-3.5 h-3.5 text-green-500 shrink-0" />;
    case 'failed':
      return <CheckCircle2 className="w-3.5 h-3.5 text-red-500 shrink-0" />;
    case 'working':
      return <Loader2 className="w-3.5 h-3.5 text-blue-500 animate-spin shrink-0" />;
    default:
      return <Circle className="w-3.5 h-3.5 text-muted-foreground/40 shrink-0" />;
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
      <div className="flex items-center gap-1.5 text-muted-foreground/70 mb-1">
        {allDone ? (
          <CheckCircle2 className="w-3 h-3 text-green-500 shrink-0" />
        ) : (
          <Loader2 className="w-3 h-3 animate-spin shrink-0" />
        )}
        <span className="font-medium">{source}</span>
        <span className="text-[10px] opacity-60">{finished}/{steps.length}</span>
      </div>
      <ul className="ml-4 space-y-1 border-l border-border/50 pl-2.5">
        {steps.map((step, i) => (
          <StepItem key={i} step={step} />
        ))}
      </ul>
    </li>
  );
}

export function WorkingBlock({ steps, complete }: WorkingBlockProps) {
  const [open, setOpen] = useState(!complete);

  const { topLevel, grouped } = useMemo(() => {
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
    return { topLevel: top, grouped: bySource };
  }, [steps]);

  if (steps.length === 0) return null;

  const finished = steps.filter((s) => s.state === 'completed' || s.state === 'failed').length;

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="my-1">
      <CollapsibleTrigger className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors py-1 cursor-pointer">
        <ChevronRight
          className={cn(
            'w-3 h-3 transition-transform duration-200',
            open && 'rotate-90'
          )}
        />
        {complete ? (
          <>
            <CheckCircle2 className="w-3 h-3 text-green-500" />
            <span>Worked — {steps.length} {steps.length === 1 ? 'step' : 'steps'}</span>
          </>
        ) : (
          <>
            <Loader2 className="w-3 h-3 animate-spin" />
            <span>Working… {finished}/{steps.length}</span>
          </>
        )}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ul className="ml-5 mt-1 mb-2 space-y-1 border-l border-border pl-3 text-xs text-muted-foreground">
          {topLevel.map((step, i) => (
            <StepItem key={i} step={step} />
          ))}
          {[...grouped.entries()].map(([source, sourceSteps]) => (
            <SourceGroup key={source} source={source} steps={sourceSteps} />
          ))}
        </ul>
      </CollapsibleContent>
    </Collapsible>
  );
}
