import { useState } from 'react';
import { Sparkles } from 'lucide-react';
import { deriveObjectId, useNannosChanges, type RouteId } from '@nannos/embed-sdk';
import { cn } from '@/lib/utils';
import { consoleObjectTypes } from './consoleObjects';

type AssistantChangesBarProps = {
  /** The same type/id/parentId the form's `<NannosForm>` registers with. */
  type: keyof typeof consoleObjectTypes & string;
  id: RouteId;
  parentId?: RouteId;
  /** `box` sits among a form's controls; `strip` spans a panel edge to edge, under its header. */
  variant?: 'box' | 'strip';
  className?: string;
};

// The marks' own colour (set by the SDK's change-mark stylesheet), so the bar, the
// field rings and the "Set by Nannos" notes read as one thing.
const MARK = 'var(--nannos-mark-a,#6d4aff)';

/**
 * "Nannos changed 3 fields · not saved · Undo all", next to a long form's Save — the
 * changed fields can be off screen there, and this answers "what am I about to save?".
 * Renders nothing while the assistant has no unsaved change on the form.
 */
export function AssistantChangesBar({ type, id, parentId, variant = 'box', className }: AssistantChangesBarProps) {
  const definition = consoleObjectTypes[type];
  const { changes, undoAll } = useNannosChanges({ type, id: deriveObjectId(definition, id, parentId) });
  const [failed, setFailed] = useState(false);
  if (!changes.length) return null;
  const count = changes.length === 1 ? '1 field' : `${changes.length} fields`;
  return (
    <div
      role="status"
      data-nannos-ignore
      style={{ color: MARK, backgroundColor: `color-mix(in srgb, ${MARK} 7%, transparent)`, borderColor: `color-mix(in srgb, ${MARK} 25%, transparent)` }}
      className={cn(
        'flex flex-wrap items-center justify-between gap-2 text-xs',
        variant === 'strip' ? 'shrink-0 border-b px-3 py-2' : 'rounded-md border px-3 py-2',
        className
      )}
    >
      <span className="inline-flex items-center gap-1.5 font-medium">
        <Sparkles className="h-3.5 w-3.5" />
        Nannos changed {count} · not saved
      </span>
      <button
        type="button"
        className="underline underline-offset-2 hover:opacity-80"
        onClick={async () => setFailed(!(await undoAll()))}
      >
        {failed ? 'Some fields could not be undone' : 'Undo all'}
      </button>
    </div>
  );
}
