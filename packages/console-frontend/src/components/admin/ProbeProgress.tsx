import { AlertTriangle, CheckCircle2, CircleDashed, HelpCircle, Loader2, XCircle } from 'lucide-react';

import type { ProbeState, ShapeStatus } from '@/lib/probeProgress';
import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';

/**
 * Live view of a model Test (nannos#318): the registration probe replays the harness's request
 * shapes one by one, which takes several seconds, so the admin sees each shape go from pending
 * to its verdict, with the request in flight named under the running one.
 */

const ICONS: Record<ShapeStatus, { icon: typeof CheckCircle2; className: string; title: string }> = {
  pending: { icon: CircleDashed, className: 'text-muted-foreground/60', title: 'Waiting' },
  running: { icon: Loader2, className: 'text-primary animate-spin', title: 'Probing' },
  ok: { icon: CheckCircle2, className: 'text-green-600 dark:text-green-500', title: 'Accepted' },
  limited: { icon: AlertTriangle, className: 'text-amber-600 dark:text-amber-500', title: 'Recorded — the gateway routes around it' },
  failed: { icon: XCircle, className: 'text-destructive', title: 'Rejected — every agent turn needs this' },
  inconclusive: { icon: HelpCircle, className: 'text-muted-foreground', title: 'Could not be measured — re-run Test' },
};

export function ProbeProgress({ state, className }: { state: ProbeState; className?: string }) {
  if (state.ping) {
    return (
      <div className={cn('flex items-center gap-2 text-sm text-muted-foreground', className)}>
        {state.finished ? (
          state.error ? <XCircle className="h-4 w-4 text-destructive" /> : <CheckCircle2 className="h-4 w-4 text-green-600" />
        ) : (
          <span className="flex h-4 w-4 overflow-hidden">
            <Loader2 className="h-4 w-4 animate-spin" />
          </span>
        )}
        {state.finished ? (state.error ? 'Test call failed' : 'Test call succeeded') : `Testing ${state.model}…`}
      </div>
    );
  }
  const done = state.rows.filter((r) => r.status !== 'pending' && r.status !== 'running').length;
  return (
    <div className={cn('space-y-3', className)} aria-live="polite">
      <div className="space-y-1.5">
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>{state.finished ? 'Probe finished' : 'Probing the request shapes the harness sends…'}</span>
          <span>
            {done} / {state.rows.length}
          </span>
        </div>
        <Progress value={state.rows.length ? (done / state.rows.length) * 100 : 0} className="h-1.5" />
      </div>
      <ul className="space-y-1.5">
        {state.rows.map((r, i) => {
          // A reason shared with the row above (e.g. "not probed" after a cooldown) is said once.
          const detail = r.detail && r.detail !== state.rows[i - 1]?.detail ? r.detail : undefined;
          const { icon: Icon, className: iconClass, title } = ICONS[r.status];
          return (
            <li key={r.shape} className="flex gap-2 text-sm">
              {/* The box clips the spinner: a rotating icon's corners count as scrollable
                  overflow, and on the last row they toggled the window's scrollbar each turn. */}
              <span className="mt-0.5 flex h-4 w-4 flex-shrink-0 overflow-hidden">
                <Icon className={cn('h-4 w-4', iconClass)} aria-label={title} />
              </span>
              <div className="min-w-0">
                <span className={cn(r.status === 'pending' && 'text-muted-foreground')}>{r.label}</span>
                {r.step && <span className="ml-2 text-xs text-muted-foreground">{r.step}…</span>}
                {detail && r.status !== 'ok' && <p className="text-xs text-muted-foreground break-words">{detail}</p>}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
