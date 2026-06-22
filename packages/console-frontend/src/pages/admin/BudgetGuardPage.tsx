import { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2, Lock, AlertTriangle, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';

import {
  getBudgetSettings,
  getBudgetStatus,
  updateBudgetSettings,
  type BudgetSettingsUpdate,
} from '@/api/budget';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Progress } from '@/components/ui/progress';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

// The openapi client rejects with the parsed error body (e.g. {detail: "..."}), so
// String(e) yields "[object Object]". Pull out a human-readable message instead.
function errMsg(e: unknown): string {
  if (typeof e === 'string') return e;
  if (e && typeof e === 'object') {
    const o = e as Record<string, unknown>;
    const d = o.detail ?? o.message ?? o.error;
    if (typeof d === 'string') return d;
    if (d) return JSON.stringify(d);
  }
  return String(e);
}

const usd = (n: number) =>
  n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });

export function BudgetGuardPage() {
  const queryClient = useQueryClient();

  // Live spend gauge — refetch on an interval so admins see month-to-date spend move.
  const statusQuery = useQuery({
    queryKey: ['budget', 'status'],
    queryFn: getBudgetStatus,
    refetchInterval: 30_000,
  });

  const settingsQuery = useQuery({
    queryKey: ['budget', 'settings'],
    queryFn: getBudgetSettings,
  });

  // Editable form state, seeded from the fetched settings.
  const [enabled, setEnabled] = useState(true);
  const [limit, setLimit] = useState('');
  const [thresholds, setThresholds] = useState('');

  useEffect(() => {
    if (settingsQuery.data) {
      setEnabled(settingsQuery.data.enabled);
      setLimit(String(settingsQuery.data.monthly_limit_usd));
      setThresholds(settingsQuery.data.warning_thresholds.map((t) => Math.round(t * 100)).join(', '));
    }
  }, [settingsQuery.data]);

  const mutation = useMutation({
    mutationFn: (body: BudgetSettingsUpdate) => updateBudgetSettings(body),
    onSuccess: () => {
      toast.success('Budget settings saved');
      queryClient.invalidateQueries({ queryKey: ['budget'] });
    },
    onError: (e) => toast.error(errMsg(e)),
  });

  const handleSave = () => {
    const limitNum = Number(limit);
    if (!Number.isFinite(limitNum) || limitNum <= 0) {
      toast.error('Monthly limit must be a positive number');
      return;
    }
    // Accept "80, 90, 95" (percentages) and store as fractions in (0, 1].
    const parsed = thresholds
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => Number(s) / 100);
    if (parsed.some((t) => !Number.isFinite(t) || t <= 0 || t > 1)) {
      toast.error('Warning thresholds must be percentages between 1 and 100');
      return;
    }
    mutation.mutate({
      enabled,
      monthly_limit_usd: limitNum,
      warning_thresholds: parsed,
    });
  };

  const status = statusQuery.data;
  const locked = status?.is_locked ?? false;
  const pct = status?.usage_percentage ?? 0;

  return (
    <div className="container mx-auto max-w-3xl space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Budget Guard</h1>
        <p className="text-muted-foreground">
          Cap global LLM spend per calendar month. Spend is summed from the gateway usage logs;
          when the limit is reached, the orchestrator rejects new requests until the next month or
          an increased limit.
        </p>
      </div>

      {/* Live status gauge */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {locked ? (
              <Lock className="h-5 w-5 text-destructive" />
            ) : (
              <ShieldCheck className="h-5 w-5 text-emerald-600" />
            )}
            Current month
          </CardTitle>
          <CardDescription>
            {status
              ? `${usd(status.spend_usd)} of ${usd(status.limit_usd)} used`
              : 'Loading current spend…'}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {statusQuery.isLoading ? (
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          ) : statusQuery.isError ? (
            <p className="text-sm text-destructive">Failed to load status: {errMsg(statusQuery.error)}</p>
          ) : (
            <>
              <Progress value={Math.min(pct, 100)} />
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">{pct.toFixed(1)}% of limit</span>
                {locked ? (
                  <span className="flex items-center gap-1 font-medium text-destructive">
                    <Lock className="h-4 w-4" /> Locked — requests rejected
                  </span>
                ) : status && status.warnings.length > 0 ? (
                  <span className="flex items-center gap-1 font-medium text-amber-600">
                    <AlertTriangle className="h-4 w-4" /> Over{' '}
                    {Math.round(Math.max(...status.warnings) * 100)}% threshold
                  </span>
                ) : !status?.enabled ? (
                  <span className="text-muted-foreground">Enforcement disabled</span>
                ) : (
                  <span className="text-emerald-600">Healthy</span>
                )}
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {/* Settings form */}
      <Card>
        <CardHeader>
          <CardTitle>Settings</CardTitle>
          <CardDescription>Changes apply within one poll interval (~5 min).</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="flex items-center justify-between">
            <div>
              <Label htmlFor="budget-enabled">Enforcement enabled</Label>
              <p className="text-sm text-muted-foreground">
                When off, spend is still tracked but requests are never blocked.
              </p>
            </div>
            <Switch id="budget-enabled" checked={enabled} onCheckedChange={setEnabled} />
          </div>

          <div className="space-y-2">
            <Label htmlFor="budget-limit">Monthly limit (USD)</Label>
            <Input
              id="budget-limit"
              type="number"
              min="0"
              step="1"
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
              className="max-w-xs"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="budget-thresholds">Warning thresholds (%)</Label>
            <Input
              id="budget-thresholds"
              value={thresholds}
              onChange={(e) => setThresholds(e.target.value)}
              placeholder="80, 90, 95"
              className="max-w-xs"
            />
            <p className="text-sm text-muted-foreground">
              Comma-separated percentages of the limit at which to surface warnings.
            </p>
          </div>

          <Button onClick={handleSave} disabled={mutation.isPending || settingsQuery.isLoading}>
            {mutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Save changes
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
