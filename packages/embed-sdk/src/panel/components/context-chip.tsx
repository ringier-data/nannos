/**
 * A host-injected user message (`metadata.display`) rendered as a muted,
 * centered context chip instead of a chat bubble. Expands (native
 * details/summary — no state to manage) to reveal the raw prompt text that was
 * actually sent to the agent.
 */
import { ChevronDownIcon } from 'lucide-react';
import { cn } from '../../lib/utils';
import { format, useStrings } from '../../react';
import type { NannosUIMessage } from '../../transport';

export interface ContextChipProps {
  message: NannosUIMessage;
  className?: string;
}

export function ContextChip({ message, className }: ContextChipProps) {
  const strings = useStrings();
  const label = message.metadata?.display?.label ?? '';
  const rawText = message.parts
    .filter((part): part is Extract<typeof part, { type: 'text' }> => part.type === 'text')
    .map((part) => part.text)
    .join('\n');

  return (
    <div className={cn('flex w-full justify-center', className)}>
      <details
        data-slot="nannos-context-chip"
        className="group max-w-[90%] rounded-lg border border-dashed bg-muted/40 text-muted-foreground text-xs"
      >
        <summary className="flex cursor-pointer select-none items-center gap-1.5 px-3 py-1.5 [&::-webkit-details-marker]:hidden [list-style:none]">
          <span className="truncate">{format(strings['context.label'], { label })}</span>
          <ChevronDownIcon
            aria-hidden="true"
            className="size-3 shrink-0 transition-transform group-open:rotate-180"
          />
        </summary>
        <div className="overflow-x-auto border-t border-dashed px-3 py-2">
          <pre className="whitespace-pre-wrap break-words font-sans text-xs">{rawText}</pre>
        </div>
      </details>
    </div>
  );
}
