import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  CheckCircle2,
  AlertTriangle,
  MinusCircle,
  RefreshCw,
  ChevronDown,
  ChevronRight,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { TableSkeleton } from '@/components/skeletons';
import { getSystemStatus, type FeatureStatus, type FeatureStatusLevel } from '@/api/model-gateway';

const STATUS_META: Record<
  FeatureStatusLevel,
  { label: string; icon: typeof CheckCircle2; badge: string; iconClass: string }
> = {
  ready: {
    label: 'Ready',
    icon: CheckCircle2,
    badge: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300',
    iconClass: 'text-emerald-600 dark:text-emerald-400',
  },
  // Works, with a caveat — a check (it functions) but amber (don't read it as fully ready).
  limited: {
    label: 'Limited',
    icon: CheckCircle2,
    badge: 'bg-amber-100 text-amber-900 dark:bg-amber-950/50 dark:text-amber-300',
    iconClass: 'text-amber-600 dark:text-amber-400',
  },
  degraded: {
    label: 'Degraded',
    icon: AlertTriangle,
    badge: 'bg-amber-100 text-amber-900 dark:bg-amber-950/50 dark:text-amber-300',
    iconClass: 'text-amber-600 dark:text-amber-400',
  },
  disabled: {
    label: 'Disabled',
    icon: MinusCircle,
    badge: 'bg-muted text-muted-foreground',
    iconClass: 'text-muted-foreground',
  },
};

function FeatureRow({ feature }: { feature: FeatureStatus }) {
  const meta = STATUS_META[feature.status] ?? STATUS_META.disabled;
  const Icon = meta.icon;
  // Collapse healthy rows by default; expand anything that needs attention (limited/degraded/disabled).
  const [expanded, setExpanded] = useState(feature.status !== 'ready');
  const Chevron = expanded ? ChevronDown : ChevronRight;

  return (
    <div className="border-b px-4 py-3 last:border-b-0">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3 text-left"
      >
        <Icon className={`h-5 w-5 shrink-0 ${meta.iconClass}`} />
        <span className="font-medium">{feature.name}</span>
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${meta.badge}`}>{meta.label}</span>
        <Chevron className="ml-auto h-4 w-4 shrink-0 text-muted-foreground" />
      </button>
      {expanded && (
        <div className="mt-1.5 pl-8">
          <p className="text-sm text-muted-foreground">{feature.detail}</p>
          {feature.caveat && (
            <p className="mt-1 flex gap-1.5 text-sm text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{feature.caveat}</span>
            </p>
          )}
          {feature.remediation && (
            <p className="mt-1 text-sm text-foreground/80">
              <span className="font-medium">To enable: </span>
              {feature.remediation}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function SystemStatusPage() {
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['system-status'],
    queryFn: getSystemStatus,
    staleTime: 30_000,
  });

  const features = data ?? [];
  const counts = features.reduce(
    (acc, f) => ({ ...acc, [f.status]: (acc[f.status] ?? 0) + 1 }),
    {} as Record<string, number>,
  );

  return (
    <div className="space-y-6 p-4 pb-8">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            <Activity className="h-6 w-6" />
            System Status
          </h1>
          <p className="text-muted-foreground">
            What's enabled, what's degraded, and what's required to turn each feature on.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
          <RefreshCw className={`mr-2 h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      {!isLoading && features.length > 0 && (
        <div className="flex gap-2 text-sm text-muted-foreground">
          <span>{counts.ready ?? 0} ready</span>
          <span>·</span>
          <span>{counts.limited ?? 0} limited</span>
          <span>·</span>
          <span>{counts.degraded ?? 0} degraded</span>
          <span>·</span>
          <span>{counts.disabled ?? 0} disabled</span>
        </div>
      )}

      <Card>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="p-4">
              <TableSkeleton />
            </div>
          ) : features.length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">No feature status available.</p>
          ) : (
            features.map((f) => <FeatureRow key={f.key} feature={f} />)
          )}
        </CardContent>
      </Card>
    </div>
  );
}
