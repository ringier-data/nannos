import { useState } from 'react';
import { Brain, ChevronRight, Loader2 } from 'lucide-react';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';

interface ThinkingBlockProps {
  thoughts: Array<{
    agent_name: string;
    content: string;
  }>;
  complete?: boolean;
}

export function ThinkingBlock({ thoughts, complete = false }: ThinkingBlockProps) {
  const [open, setOpen] = useState(!complete);

  if (thoughts.length === 0) return null;

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
          <Brain className="w-3 h-3" />
        ) : (
          <Loader2 className="w-3 h-3 animate-spin" />
        )}
        <span>{complete ? 'Thinking' : 'Thinking…'} {thoughts.length} {thoughts.length === 1 ? 'sub-agent' : 'sub-agents'}</span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="ml-5 mt-1 mb-2 space-y-2 border-l border-border pl-3">
          {thoughts.map((thought, index) => (
            <div key={index} className="space-y-1">
              <div className="text-xs font-medium text-muted-foreground/80">
                {thought.agent_name}
              </div>
              <div className="text-xs text-muted-foreground bg-muted/50 rounded p-2 font-mono whitespace-pre-wrap overflow-x-auto">
                {thought.content}
              </div>
            </div>
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
