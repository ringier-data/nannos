import { ArrowRight } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { getModelLabel, useAvailableModels } from '@/config/models';
import { cn } from '@/lib/utils';

interface ModelStatusTextProps {
  /** The stored model alias for the agent (null/empty = intentional "Default"). */
  value: string | null | undefined;
  /** Whether `value` has been retired from the gateway — resolved by console-backend. */
  modelRetired?: boolean;
  /** The alias the agent actually runs on when retired (the chat default) — from console-backend. */
  effectiveModel?: string | null;
  className?: string;
}

/**
 * Read-only display of a sub-agent's model. console-backend is the source of truth: when it
 * flags the stored alias as retired, this renders "<old> (retired) -> <effective> (default)"
 * instead of a stale/empty value. Available models are used only to resolve human labels.
 */
export function ModelStatusText({ value, modelRetired, effectiveModel, className }: ModelStatusTextProps) {
  const { models } = useAvailableModels();

  if (!value) {
    return <p className={cn('text-sm', className)}>Default</p>;
  }
  if (!modelRetired) {
    return <p className={cn('text-sm', className)}>{getModelLabel(value, models)}</p>;
  }

  return (
    <div className={cn('flex flex-wrap items-center gap-1.5 text-sm', className)}>
      <span className="text-muted-foreground line-through">{getModelLabel(value, models)}</span>
      <Badge variant="outline" className="border-amber-500/50 text-[10px] text-amber-600 dark:text-amber-400">
        retired
      </Badge>
      <ArrowRight className="h-3 w-3 text-muted-foreground" />
      <span>{effectiveModel ? getModelLabel(effectiveModel, models) : 'Default'}</span>
      <Badge variant="secondary" className="text-[10px]">
        default
      </Badge>
    </div>
  );
}
