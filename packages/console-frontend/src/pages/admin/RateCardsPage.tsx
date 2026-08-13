import { useState, useMemo, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Edit2, Trash2, Copy, Info, Search, ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react';
import { toast } from 'sonner';
import {
  createRateCardEntryApiV1AdminRateCardsEntryPostMutation,
  expireRateCardEntryApiV1AdminRateCardsExpireRateIdPostMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { listRateCardEntriesApiV1AdminRateCardsGet } from '@/api/generated/sdk.gen';
import type { RateCardEntry, RateCardEntryCreate } from '@/api/generated';
import { Button } from '@/components/ui/button';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Card, CardContent } from '@/components/ui/card';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { CardListSkeleton } from '@/components/skeletons';
import { Badge } from '@/components/ui/badge';
import { ProviderMismatchBanner } from '@/components/admin/ProviderMismatchBanner';
import { PROVIDER_CONFIG_QUERY_KEY } from '@/lib/providerCheckQuery';

interface GroupedModel {
  provider: string;
  model_name: string;
  model_name_pattern: string | null;
  // ALL active entries for this model (kept so edit/delete can expire every version — this is
  // what compacts the historical duplicates over time). `latestEntries` is the deduped view.
  entries: RateCardEntry[];
  latestEntries: RateCardEntry[];
  inputPrice?: number;
  outputPrice?: number;
  otherPrices: Array<{ billing_unit: string; price: number }>;
}

const PAGE_SIZE = 8;

// Fetch every active entry (the list endpoint caps at 100/page, and there can be more —
// especially with historical versions — so page through to the end). Grouping/search/pagination
// then happen client-side over models.
async function fetchAllActiveEntries(): Promise<RateCardEntry[]> {
  const limit = 100;
  const all: RateCardEntry[] = [];
  for (let page = 1; ; page++) {
    const { data, error } = await listRateCardEntriesApiV1AdminRateCardsGet({
      query: { active_only: true, page, limit },
    });
    if (error) throw error;
    const batch = data?.entries ?? [];
    all.push(...batch);
    if (batch.length === 0 || all.length >= (data?.total ?? all.length)) break;
  }
  return all;
}

const prettyUnit = (u: string) => u.split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');

export function RateCardsPage() {
  const [selectedProvider, setSelectedProvider] = useState<string>('all');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [addModelOpen, setAddModelOpen] = useState(false);
  const [editModel, setEditModel] = useState<GroupedModel | null>(null);

  const queryClient = useQueryClient();

  // One fetch of ALL active entries; provider filter, search and pagination are client-side over
  // the grouped models (so a model never gets split across a server page and silently hidden).
  const { data: entries = [], isLoading: entriesLoading } = useQuery({
    queryKey: ['rate-card-entries-all'],
    queryFn: fetchAllActiveEntries,
  });

  // Both writes change what the billing banner sees (a new card can resolve a flagged $0 deployment;
  // expiring one can create it), so its cached result has to go too — otherwise the banner keeps
  // rendering pre-fix state while the page stays mounted.
  const invalidateAfterWrite = () => {
    queryClient.invalidateQueries({ queryKey: ['rate-card-entries-all'] });
    queryClient.invalidateQueries({ queryKey: PROVIDER_CONFIG_QUERY_KEY });
  };

  const createEntryMutation = useMutation({
    ...createRateCardEntryApiV1AdminRateCardsEntryPostMutation(),
    onSuccess: () => {
      toast.success('Rate card created successfully');
      invalidateAfterWrite();
      setAddModelOpen(false);
      setEditModel(null);
    },
    onError: (error) => {
      // A 422 carries the reason (e.g. a provider the runtime never reports, which would bill $0) —
      // show it instead of a generic failure the admin can't act on.
      const detail = (error as { detail?: unknown })?.detail;
      toast.error(typeof detail === 'string' ? detail : 'Failed to create rate card entry');
    },
  });

  const expireMutation = useMutation({
    ...expireRateCardEntryApiV1AdminRateCardsExpireRateIdPostMutation(),
    onSuccess: () => {
      toast.success('Rate card entries expired');
      invalidateAfterWrite();
    },
    onError: () => {
      toast.error('Failed to expire rate cards');
    },
  });

  const allProviders = useMemo(
    () => Array.from(new Set(entries.map((e: RateCardEntry) => e.provider))).sort(),
    [entries],
  );

  // Group entries by model. Keep every entry (for expire-all on edit/delete) but build a deduped
  // `latestEntries` (one per billing_unit, newest effective_from wins) for a clean display — the
  // table can accumulate historical versions that all read as active.
  const groupedModels = useMemo(() => {
    const groups = new Map<string, GroupedModel & { _latest: Map<string, RateCardEntry> }>();

    for (const entry of entries) {
      const key = `${entry.provider}::${entry.model_name}`;
      let group = groups.get(key);
      if (!group) {
        group = {
          provider: entry.provider,
          model_name: entry.model_name,
          model_name_pattern: entry.model_name_pattern ?? null,
          entries: [],
          latestEntries: [],
          otherPrices: [],
          _latest: new Map(),
        };
        groups.set(key, group);
      }
      group.entries.push(entry);
      const prev = group._latest.get(entry.billing_unit);
      if (!prev || entry.effective_from > prev.effective_from) group._latest.set(entry.billing_unit, entry);
    }

    return Array.from(groups.values())
      .map((g) => {
        const latest = Array.from(g._latest.values());
        const input = g._latest.get('base_input_tokens');
        const output = g._latest.get('base_output_tokens');
        return {
          provider: g.provider,
          model_name: g.model_name,
          model_name_pattern: g.model_name_pattern,
          entries: g.entries,
          latestEntries: latest,
          inputPrice: input ? parseFloat(input.price_per_million) : undefined,
          outputPrice: output ? parseFloat(output.price_per_million) : undefined,
          otherPrices: latest
            .filter((e) => e.billing_unit !== 'base_input_tokens' && e.billing_unit !== 'base_output_tokens')
            .map((e) => ({ billing_unit: e.billing_unit, price: parseFloat(e.price_per_million) })),
        } as GroupedModel;
      })
      .sort((a, b) => `${a.provider}/${a.model_name}`.localeCompare(`${b.provider}/${b.model_name}`));
  }, [entries]);

  // Provider filter + free-text search (model name or provider), then paginate the model cards.
  const filteredModels = useMemo(() => {
    const q = search.trim().toLowerCase();
    return groupedModels.filter(
      (m) =>
        (selectedProvider === 'all' || m.provider === selectedProvider) &&
        (!q || m.model_name.toLowerCase().includes(q) || m.provider.toLowerCase().includes(q)),
    );
  }, [groupedModels, selectedProvider, search]);

  const totalPages = Math.max(1, Math.ceil(filteredModels.length / PAGE_SIZE));
  const pageModels = filteredModels.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  // Snap back to a valid page when filters/search shrink the result set.
  useEffect(() => {
    if (page > totalPages) setPage(1);
  }, [page, totalPages]);

  const handleExpireModel = (model: GroupedModel) => {
    const effectiveUntil = new Date().toISOString();
    
    // Expire all entries for this model
    Promise.all(
      model.entries.map(entry =>
        expireMutation.mutateAsync({
          path: { rate_id: entry.id },
          query: { effective_until: effectiveUntil },
        })
      )
    ).catch(() => {
      // Error already handled by mutation
    });
  };

  const handleCopyModel = (model: GroupedModel) => {
    // Open add dialog with pre-filled data from this model
    setEditModel(model);
    setAddModelOpen(true);
  };

  return (
    <div className="container mx-auto p-6 space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold">Rate Cards</h1>
          <p className="text-muted-foreground mt-1">Manage billing unit pricing for cost calculation</p>
        </div>
        <Button onClick={() => { setEditModel(null); setAddModelOpen(true); }}>
          <Plus className="w-4 h-4 mr-2" />
          Add Model Pricing
        </Button>
      </div>

      {/* Live billing-configuration check (async — never blocks the list) */}
      <ProviderMismatchBanner />

      {/* Info Banner */}
      <Card className="bg-blue-50/50 dark:bg-blue-950/20 border-blue-200 dark:border-blue-800">
        <CardContent className="pt-0">
          <div className="flex gap-3">
            <Info className="w-5 h-5 text-blue-600 dark:text-blue-400 mt-0.5 flex-shrink-0" />
            <div className="space-y-1">
              <p className="text-sm font-medium text-blue-900 dark:text-blue-100">
                Model Variant Matching
              </p>
              <p className="text-sm text-blue-800 dark:text-blue-200">
                Rate cards support regex patterns to match model variants automatically. For example, <code className="text-xs bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">^gpt-4o-mini(-\d{'{4}'}-\d{'{2}'}-\d{'{2}'})?$</code> matches both <code className="text-xs bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">gpt-4o-mini</code> and <code className="text-xs bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">gpt-4o-mini-2024-07-18</code>.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Filters: free-text search + provider */}
      <div className="flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            placeholder="Search by model or provider…"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(1); }}
            className="pl-9"
          />
        </div>
        <div className="w-full sm:w-56">
          <Select value={selectedProvider} onValueChange={(v) => { setSelectedProvider(v); setPage(1); }}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All providers</SelectItem>
              {allProviders.map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Compact, collapsible model cards (paginated) */}
      {entriesLoading ? (
        <CardListSkeleton />
      ) : filteredModels.length === 0 ? (
        <Card>
          <CardContent className="pt-6">
            <div className="text-center text-muted-foreground">
              {groupedModels.length === 0
                ? 'No rate cards found. Add a model to get started.'
                : 'No models match your filters.'}
            </div>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="text-xs text-muted-foreground">
            {filteredModels.length} model{filteredModels.length === 1 ? '' : 's'}
          </div>
          <div className="space-y-2">
            {pageModels.map((model) => (
              <RateCardModelCard
                key={`${model.provider}::${model.model_name}`}
                model={model}
                onEdit={() => { setEditModel(model); setAddModelOpen(true); }}
                onCopy={() => handleCopyModel(model)}
                onDelete={() => handleExpireModel(model)}
              />
            ))}
          </div>
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-3 pt-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                <ChevronLeft className="w-4 h-4" />
              </Button>
              <span className="text-sm text-muted-foreground">Page {page} of {totalPages}</span>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                <ChevronRight className="w-4 h-4" />
              </Button>
            </div>
          )}
        </>
      )}

      {/* Add/Edit Model Dialog */}
      <ModelPricingDialog
        open={addModelOpen}
        onOpenChange={(open) => {
          setAddModelOpen(open);
          if (!open) setEditModel(null);
        }}
        onSubmit={async (entries) => {
          // Create BEFORE expiring, never the other way round. The create can fail — most sharply on
          // a card keyed to a provider the runtime never reports (422), which is exactly the kind of
          // card the banner tells admins to come and fix — and expiring first would then leave the
          // model with zero active pricing, i.e. billing $0, with the provider field disabled so it
          // couldn't even be corrected here. In this order a failed create leaves the old rates in
          // place; the new entry simply wins while both are open (get_active_rate takes the latest
          // effective_from), and the expiry below is cleanup rather than a prerequisite.
          const createdIds = new Set<number>();
          for (const entry of entries) {
            const created = await createEntryMutation.mutateAsync({ body: entry });
            const id = (created as { id?: number } | undefined)?.id;
            if (typeof id === 'number') createdIds.add(id);
          }

          if (editModel) {
            const effectiveUntil = new Date().toISOString();
            await Promise.all(
              editModel.entries
                // An unchanged price is a no-op server-side: create_entry returns the EXISTING entry
                // id instead of inserting a row. Expiring that id would close the very entry we just
                // "created" and leave the unit unpriced — so skip anything a create just claimed.
                .filter((entry) => !createdIds.has(entry.id))
                .map(entry =>
                  expireMutation.mutateAsync({
                    path: { rate_id: entry.id },
                    query: { effective_until: effectiveUntil },
                  })
                )
            );
          }
        }}
        existingModel={editModel}
      />
    </div>
  );
}

// A single compact, collapsible rate-card model row. Collapsed shows a one-line price summary;
// expanded reveals the full base + additional-unit-type breakdown and (via the header) actions.
function RateCardModelCard({
  model,
  onEdit,
  onCopy,
  onDelete,
}: {
  model: GroupedModel;
  onEdit: () => void;
  onCopy: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);

  const summary: string[] = [];
  if (model.inputPrice !== undefined) summary.push(`in $${model.inputPrice.toFixed(2)}`);
  if (model.outputPrice !== undefined) summary.push(`out $${model.outputPrice.toFixed(2)}`);
  if (model.otherPrices.length) summary.push(`+${model.otherPrices.length} more`);

  // Full version history per billing unit (newest first) for the expanded view. The current
  // version of each unit is the one in `latestEntries` (latest effective_from); the rest are
  // prior rates kept for historical billing.
  const currentIds = new Set(model.latestEntries.map((e) => e.id));
  const unitOrder = (u: string) => (u === 'base_input_tokens' ? 0 : u === 'base_output_tokens' ? 1 : 2);
  const history = Array.from(
    model.entries.reduce((map, e) => {
      (map.get(e.billing_unit) ?? map.set(e.billing_unit, []).get(e.billing_unit)!).push(e);
      return map;
    }, new Map<string, RateCardEntry[]>()),
  )
    .map(([unit, versions]) => ({
      unit,
      flow: versions[0].flow_direction,
      versions: [...versions].sort((a, b) => (a.effective_from < b.effective_from ? 1 : -1)),
    }))
    .sort((a, b) => unitOrder(a.unit) - unitOrder(b.unit) || a.unit.localeCompare(b.unit));

  return (
    <Card>
      <Collapsible open={open} onOpenChange={setOpen}>
        <div className="flex items-center justify-between gap-3 p-3">
          <CollapsibleTrigger className="flex items-center gap-3 flex-1 min-w-0 text-left">
            <ChevronDown
              className={`w-4 h-4 shrink-0 text-muted-foreground transition-transform ${open ? '' : '-rotate-90'}`}
            />
            <Badge variant="outline" className="shrink-0">{model.provider}</Badge>
            <span className="font-mono text-sm truncate">{model.model_name}</span>
            {model.model_name_pattern && (
              <Badge variant="secondary" className="shrink-0 text-[10px]">pattern</Badge>
            )}
            <span className="text-xs text-muted-foreground truncate ml-1">
              {summary.join(' · ') || 'No pricing set'}
            </span>
          </CollapsibleTrigger>
          <div className="flex gap-1 shrink-0">
            <Button variant="ghost" size="sm" onClick={onEdit} aria-label="Edit"><Edit2 className="w-4 h-4" /></Button>
            <Button variant="ghost" size="sm" onClick={onCopy} aria-label="Copy"><Copy className="w-4 h-4" /></Button>
            <Button variant="ghost" size="sm" onClick={onDelete} aria-label="Delete">
              <Trash2 className="w-4 h-4 text-destructive" />
            </Button>
          </div>
        </div>
        <CollapsibleContent>
          <div className="border-t px-4 py-4 space-y-4">
            {model.model_name_pattern && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <span>Matches pattern:</span>
                <code className="text-xs bg-muted px-2 py-1 rounded">{model.model_name_pattern}</code>
              </div>
            )}
            <div className="text-sm font-medium text-muted-foreground">Pricing history</div>
            <div className="space-y-3">
              {history.map(({ unit, flow, versions }) => {
                const flowText =
                  flow === 'input'
                    ? 'text-blue-700 dark:text-blue-400'
                    : flow === 'output'
                    ? 'text-green-700 dark:text-green-400'
                    : 'text-muted-foreground';
                return (
                  <div key={unit}>
                    <div className={`text-xs font-medium mb-1 ${flowText}`}>{prettyUnit(unit)}</div>
                    <div className="space-y-1">
                      {versions.map((v) => {
                        const isCurrent = currentIds.has(v.id);
                        return (
                          <div
                            key={v.id}
                            className={`flex items-center justify-between gap-3 rounded-md border px-3 py-1.5 text-sm ${
                              isCurrent ? 'bg-muted/40' : 'opacity-60'
                            }`}
                          >
                            <span className="font-mono">
                              ${parseFloat(v.price_per_million).toFixed(2)}
                              <span className="text-xs text-muted-foreground"> /1M</span>
                            </span>
                            <span className="flex-1 text-right text-xs text-muted-foreground">
                              from {new Date(v.effective_from).toLocaleDateString()}
                              {v.effective_until ? ` · until ${new Date(v.effective_until).toLocaleDateString()}` : ''}
                            </span>
                            {isCurrent ? (
                              <Badge variant="secondary" className="shrink-0 text-[10px]">current</Badge>
                            ) : (
                              <span className="shrink-0 text-[10px] text-muted-foreground">superseded</span>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

// Model Pricing Dialog Component - Handles both add and edit
function ModelPricingDialog({ open, onOpenChange, onSubmit, existingModel }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (entries: RateCardEntryCreate[]) => Promise<void>;
  existingModel?: GroupedModel | null;
}) {
  const isEdit = !!existingModel;
  
  const [formData, setFormData] = useState({
    provider: '',
    model_name: '',
    model_name_pattern: '',
    input_price: '',
    output_price: '',
    input_breakdown: [] as Array<{ billing_unit: string; price: string }>,
    output_breakdown: [] as Array<{ billing_unit: string; price: string }>,
  });

  // Reset or pre-fill form when dialog opens/changes
  useEffect(() => {
    if (open) {
      if (existingModel) {
        // Categorize other prices into input/output breakdown based on flow_direction
        const inputBreakdown: Array<{ billing_unit: string; price: string }> = [];
        const outputBreakdown: Array<{ billing_unit: string; price: string }> = [];
        
        // Deduped latest entries (the raw list can hold many historical versions per unit).
        existingModel.latestEntries.forEach(entry => {
          // Skip base input/output prices as they're handled separately
          if (entry.billing_unit === 'base_input_tokens' || entry.billing_unit === 'base_output_tokens') {
            return;
          }
          
          if (entry.flow_direction === 'input') {
            inputBreakdown.push({ 
              billing_unit: entry.billing_unit, 
              price: parseFloat(entry.price_per_million).toString() 
            });
          } else if (entry.flow_direction === 'output') {
            outputBreakdown.push({ 
              billing_unit: entry.billing_unit, 
              price: parseFloat(entry.price_per_million).toString() 
            });
          }
        });

        setFormData({
          provider: existingModel.provider,
          model_name: existingModel.model_name,
          model_name_pattern: existingModel.model_name_pattern || '',
          input_price: existingModel.inputPrice?.toString() || '',
          output_price: existingModel.outputPrice?.toString() || '',
          input_breakdown: inputBreakdown,
          output_breakdown: outputBreakdown,
        });
      } else {
        setFormData({
          provider: '',
          model_name: '',
          model_name_pattern: '',
          input_price: '',
          output_price: '',
          input_breakdown: [],
          output_breakdown: [],
        });
      }
    }
  }, [open, existingModel]);

  const addInputBreakdown = () => {
    setFormData({
      ...formData,
      input_breakdown: [...formData.input_breakdown, { billing_unit: '', price: '' }],
    });
  };

  const removeInputBreakdown = (index: number) => {
    setFormData({
      ...formData,
      input_breakdown: formData.input_breakdown.filter((_, i) => i !== index),
    });
  };

  const updateInputBreakdown = (index: number, field: 'billing_unit' | 'price', value: string) => {
    const updated = [...formData.input_breakdown];
    updated[index] = { ...updated[index], [field]: value };
    setFormData({ ...formData, input_breakdown: updated });
  };

  const addOutputBreakdown = () => {
    setFormData({
      ...formData,
      output_breakdown: [...formData.output_breakdown, { billing_unit: '', price: '' }],
    });
  };

  const removeOutputBreakdown = (index: number) => {
    setFormData({
      ...formData,
      output_breakdown: formData.output_breakdown.filter((_, i) => i !== index),
    });
  };

  const updateOutputBreakdown = (index: number, field: 'billing_unit' | 'price', value: string) => {
    const updated = [...formData.output_breakdown];
    updated[index] = { ...updated[index], [field]: value };
    setFormData({ ...formData, output_breakdown: updated });
  };

  const handleSubmit = async () => {
    const entries: RateCardEntryCreate[] = [];

    // Add input price if provided
    if (formData.input_price) {
      entries.push({
        provider: formData.provider,
        model_name: formData.model_name,
        billing_unit: 'base_input_tokens',
        flow_direction: 'input',
        price_per_million: parseFloat(formData.input_price),
      });
    }

    // Add output price if provided
    if (formData.output_price) {
      entries.push({
        provider: formData.provider,
        model_name: formData.model_name,
        billing_unit: 'base_output_tokens',
        flow_direction: 'output',
        price_per_million: parseFloat(formData.output_price),
      });
    }

    // Add input breakdown types
    formData.input_breakdown.forEach((type) => {
      if (type.billing_unit && type.price) {
        entries.push({
          provider: formData.provider,
          model_name: formData.model_name,
          billing_unit: type.billing_unit,
          flow_direction: 'input',
          price_per_million: parseFloat(type.price),
        });
      }
    });

    // Add output breakdown types
    formData.output_breakdown.forEach((type) => {
      if (type.billing_unit && type.price) {
        entries.push({
          provider: formData.provider,
          model_name: formData.model_name,
          billing_unit: type.billing_unit,
          flow_direction: 'output',
          price_per_million: parseFloat(type.price),
        });
      }
    });

    if (entries.length === 0) {
      toast.error('Please provide at least one price');
      return;
    }

    // Keep the dialog open when a write fails, so the entered prices aren't lost and the toast's
    // reason (e.g. a provider the runtime never reports) can be acted on. The mutations report the
    // failure themselves; swallowing it here only stops the unhandled rejection.
    try {
      await onSubmit(entries);
    } catch {
      return;
    }
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit' : 'Add'} Model Pricing</DialogTitle>
          <DialogDescription>
            {isEdit 
              ? 'Update pricing for this model. Changes will expire old rates and create new ones.'
              : 'Set pricing for a new model. Provide input/output prices and optionally add other token types.'
            }
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          {/* Provider and Model Name */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="provider">
                Provider
                {/* The card only bills when this equals the provider FAMILY the gateway reports at
                    runtime (bedrock, vertex_ai, …). A LiteLLM catalog tag (bedrock_converse) or a
                    Vertex location (eu) never matches usage — the server rejects those with a 422,
                    but the hint keeps admins from hitting it. */}
                <span className="text-xs text-muted-foreground ml-2">
                  Runtime provider family, not a catalog tag or region
                </span>
              </Label>
              <Input
                id="provider"
                value={formData.provider}
                onChange={(e) => setFormData({ ...formData, provider: e.target.value })}
                placeholder="e.g., bedrock, vertex_ai, azure"
                disabled={isEdit}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="model_name">Model Name</Label>
              <Input
                id="model_name"
                value={formData.model_name}
                onChange={(e) => setFormData({ ...formData, model_name: e.target.value })}
                placeholder="e.g., claude-sonnet-4-20250514"
                disabled={isEdit}
              />
            </div>
          </div>

          {/* Model Name Pattern */}
          <div className="space-y-2">
            <Label htmlFor="model_name_pattern">
              Model Name Pattern (Optional)
              <span className="text-xs text-muted-foreground ml-2">Regex pattern for matching model variants</span>
            </Label>
            <Input
              id="model_name_pattern"
              value={formData.model_name_pattern}
              onChange={(e) => setFormData({ ...formData, model_name_pattern: e.target.value })}
              placeholder="e.g., ^gpt-4o-mini(-\d{4}-\d{2}-\d{2})?$"
              disabled={isEdit}
            />
            <p className="text-xs text-muted-foreground">
              Leave empty for exact match only. Use regex to match multiple model versions (e.g., gpt-4o-mini-2024-07-18).
            </p>
          </div>

          {/* Input Pricing Section */}
          <div className="space-y-3 border rounded-lg p-4 bg-muted/30">
            <div className="flex items-center justify-between">
              <Label className="text-base font-semibold">Input Pricing</Label>
            </div>
            <div className="space-y-2">
              <Label htmlFor="input_price">Base Input Price (per 1M units)</Label>
              <Input
                id="input_price"
                type="number"
                step="0.01"
                value={formData.input_price}
                onChange={(e) => setFormData({ ...formData, input_price: e.target.value })}
                placeholder="3.00"
              />
            </div>
            
            {/* Input Breakdown */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label className="text-sm text-muted-foreground">Additional Input Types</Label>
                <Button type="button" variant="outline" size="sm" onClick={addInputBreakdown}>
                  <Plus className="w-3 h-3 mr-1" />
                  Add
                </Button>
              </div>
              {formData.input_breakdown.length > 0 && (
                <div className="space-y-2">
                  {formData.input_breakdown.map((type, index) => (
                    <div key={index} className="flex gap-2">
                      <div className="flex-1">
                        <Input
                          value={type.billing_unit}
                          onChange={(e) => updateInputBreakdown(index, 'billing_unit', e.target.value)}
                          placeholder="e.g., cache_creation"
                          className="text-sm"
                        />
                      </div>
                      <div className="flex-1">
                        <Input
                          type="number"
                          step="0.01"
                          value={type.price}
                          onChange={(e) => updateInputBreakdown(index, 'price', e.target.value)}
                          placeholder="Price per 1M"
                          className="text-sm"
                        />
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => removeInputBreakdown(index)}
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Output Pricing Section */}
          <div className="space-y-3 border rounded-lg p-4 bg-muted/30">
            <div className="flex items-center justify-between">
              <Label className="text-base font-semibold">Output Pricing</Label>
            </div>
            <div className="space-y-2">
              <Label htmlFor="output_price">Base Output Price (per 1M units)</Label>
              <Input
                id="output_price"
                type="number"
                step="0.01"
                value={formData.output_price}
                onChange={(e) => setFormData({ ...formData, output_price: e.target.value })}
                placeholder="15.00"
              />
            </div>
            
            {/* Output Breakdown */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label className="text-sm text-muted-foreground">Additional Output Types</Label>
                <Button type="button" variant="outline" size="sm" onClick={addOutputBreakdown}>
                  <Plus className="w-3 h-3 mr-1" />
                  Add
                </Button>
              </div>
              {formData.output_breakdown.length > 0 && (
                <div className="space-y-2">
                  {formData.output_breakdown.map((type, index) => (
                    <div key={index} className="flex gap-2">
                      <div className="flex-1">
                        <Input
                          value={type.billing_unit}
                          onChange={(e) => updateOutputBreakdown(index, 'billing_unit', e.target.value)}
                          placeholder="e.g., cache_read"
                          className="text-sm"
                        />
                      </div>
                      <div className="flex-1">
                        <Input
                          type="number"
                          step="0.01"
                          value={type.price}
                          onChange={(e) => updateOutputBreakdown(index, 'price', e.target.value)}
                          placeholder="Price per 1M"
                          className="text-sm"
                        />
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => removeOutputBreakdown(index)}
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={handleSubmit}>{isEdit ? 'Update' : 'Create'} Pricing</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
