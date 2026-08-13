import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

interface TooltipIconButtonProps {
  icon: ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
  side?: 'top' | 'bottom' | 'left' | 'right';
}

/**
 * Small icon-only action button (message-row actions: copy, feedback, report)
 * with a tooltip carrying its label. Defaults to side="bottom" since these
 * buttons typically sit just below a message bubble.
 */
export function TooltipIconButton({ icon, label, onClick, disabled, className, side = 'bottom' }: TooltipIconButtonProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          disabled={disabled}
          className={cn(
            'p-1 rounded text-muted-foreground/50 hover:text-muted-foreground hover:bg-accent transition-colors',
            className
          )}
          aria-label={label}
        >
          {icon}
        </button>
      </TooltipTrigger>
      <TooltipContent side={side} sideOffset={4}>
        {label}
      </TooltipContent>
    </Tooltip>
  );
}
