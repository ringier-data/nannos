import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, ArrowDown, Plus, ShieldCheck, X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';

import { listAvailableModels, listTierGroups, setTierFailoverChain } from '@/api/model-gateway';
import type { TierGroup } from '@/api/model-gateway';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/**
 * Failover chains per chat tier (nannos#204).
 *
 * A tier group is one chat tier's ordered models: its default first, then the models the
 * gateway tries when the default's provider is unavailable. Nannos only declares the chain —
 * LiteLLM executes it (retries, cooldown, then the next model in the chain), so nothing here
 * runs at request time.
 *
 * Two things this UI deliberately makes visible rather than smoothing over:
 *   - the head is NOT editable here. It is the tier's default, set on the model cards below,
 *     so there is exactly one place a tier's primary model is chosen.
 *   - drift against the live proxy is shown as a warning. A chain that exists only in the
 *     console is a failover that will not happen, which is the precise failure this feature
 *     exists to prevent.
 *
 * Chat tiers only: the backend refuses embedding roles, because a failed-over embedding call
 * writes vectors from another embedding space into the same index and quietly degrades search.
 */

const TIER_LABEL: Record<string, string> = {
  chat: 'Standard',
  'chat:low': 'Low',
  'chat:premium': 'Premium',
};

function TierRow({
  group,
  candidates,
  onSave,
  saving,
}: {
  group: TierGroup;
  candidates: string[];
  onSave: (role: string, fallbacks: string[]) => void;
  saving: boolean;
}) {
  const [adding, setAdding] = useState(false);
  const chain = group.fallbacks ?? [];
  // A chain may not revisit a model it has already tried — repeating one just retries a
  // provider that is, by then, known to be unavailable. The backend enforces this too.
  const used = new Set([group.default ?? '', ...chain]);
  const selectable = candidates.filter((m) => !used.has(m));

  if (!group.default) {
    return (
      <div className="rounded-md border border-dashed p-3">
        <div className="flex items-center gap-2">
          <span className="font-medium">{TIER_LABEL[group.role] ?? group.role}</span>
          <span className="text-muted-foreground text-sm">
            No default model — set one before giving this tier a failover chain.
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-md border p-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="font-medium">{TIER_LABEL[group.role] ?? group.role}</span>
        {group.gateway_mismatch != null && (
          <Badge variant="destructive" className="gap-1">
            <AlertTriangle className="h-3 w-3" />
            Not what the gateway holds
          </Badge>
        )}
      </div>

      {group.gateway_mismatch != null && (
        <p className="text-destructive text-xs">
          The gateway is routing {group.gateway_mismatch.length === 0
            ? 'no failover at all'
            : `to ${group.gateway_mismatch.join(' → ')}`}
          . Re-save this chain to re-declare it.
        </p>
      )}

      <ol className="space-y-1">
        <li className="flex items-center gap-2 text-sm">
          <Badge variant="secondary">1</Badge>
          <span className="font-mono">{group.default}</span>
          <span className="text-muted-foreground text-xs">default</span>
        </li>
        {chain.map((alias, i) => (
          <li key={alias} className="flex items-center gap-2 text-sm">
            <ArrowDown className="text-muted-foreground h-3 w-3" />
            <Badge variant="outline">{i + 2}</Badge>
            <span className="font-mono">{alias}</span>
            <Button
              size="sm"
              variant="ghost"
              disabled={saving}
              onClick={() => onSave(group.role, chain.filter((a) => a !== alias))}
              aria-label={`Remove ${alias} from the ${group.role} chain`}
            >
              <X className="h-3 w-3" />
            </Button>
          </li>
        ))}
      </ol>

      {adding && selectable.length > 0 ? (
        <Select
          onValueChange={(alias) => {
            setAdding(false);
            onSave(group.role, [...chain, alias]);
          }}
        >
          <SelectTrigger className="w-72">
            <SelectValue placeholder="Add a model to the chain…" />
          </SelectTrigger>
          <SelectContent>
            {selectable.map((m) => (
              <SelectItem key={m} value={m}>
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        <Button
          size="sm"
          variant="outline"
          disabled={saving || selectable.length === 0}
          onClick={() => setAdding(true)}
        >
          <Plus className="mr-1 h-3 w-3" />
          {selectable.length === 0 ? 'No other models registered' : 'Add fallback'}
        </Button>
      )}
    </div>
  );
}

export function FailoverChains() {
  const queryClient = useQueryClient();

  const { data: groups, isLoading } = useQuery({
    queryKey: ['gateway-tier-groups'],
    queryFn: listTierGroups,
  });
  const { data: available } = useQuery({
    queryKey: ['available-models'],
    queryFn: listAvailableModels,
  });

  const mutation = useMutation({
    mutationFn: ({ role, fallbacks }: { role: string; fallbacks: string[] }) =>
      setTierFailoverChain(role, fallbacks),
    onSuccess: () => {
      toast.success('Failover chain updated on the gateway');
      queryClient.invalidateQueries({ queryKey: ['gateway-tier-groups'] });
    },
    onError: (e: unknown) => toast.error(`Failover chain not saved: ${String(e)}`),
  });

  const candidates = (available ?? []).map((m) => m.model_name).filter(Boolean) as string[];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <ShieldCheck className="h-4 w-4" />
          Failover
        </CardTitle>
        <CardDescription>
          What each chat tier falls back to when its provider is unavailable. The gateway retries,
          cools the failing model down, then walks the chain — callers keep asking for the tier's
          default and never see the switch. Embeddings never fail over: a second model's vectors
          would silently corrupt the search index.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <p className="text-muted-foreground text-sm">Loading…</p>
        ) : (
          (groups ?? []).map((g) => (
            <TierRow
              key={g.role}
              group={g}
              candidates={candidates}
              saving={mutation.isPending}
              onSave={(role, fallbacks) => mutation.mutate({ role, fallbacks })}
            />
          ))
        )}
      </CardContent>
    </Card>
  );
}
