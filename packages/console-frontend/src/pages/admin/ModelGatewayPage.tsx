import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, FlaskConical, Cpu, Eye, Brain, Star, Pencil, Loader2, Lock } from 'lucide-react';
import { toast } from 'sonner';

import {
  listGatewayModels,
  listModelCatalog,
  registerGatewayModel,
  updateGatewayModel,
  testGatewayModel,
  deleteGatewayModel,
  setGatewayModelDefault,
  getCostPrefill,
  type CatalogModel,
  type DefaultRole,
  type GatewayModel,
  type ModelRegistrationRequest,
  type RateCardPricingEntry,
} from '@/api/model-gateway';
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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

const ALL_INPUT_MODES = ['text', 'image', 'audio', 'video', 'file'] as const;

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

// billing_unit -> flow_direction. These match the units the proxy CustomLogger emits.
// `embeddingOnly` units only apply to embedding models (e.g. multimodal image inputs).
const PRICING_UNITS: Array<{
  unit: string;
  label: string;
  flow: RateCardPricingEntry['flow_direction'];
  embeddingOnly?: boolean;
}> = [
  { unit: 'base_input_tokens', label: 'Input ($/M tokens)', flow: 'input' },
  { unit: 'base_output_tokens', label: 'Output ($/M tokens)', flow: 'output' },
  { unit: 'cache_read_input_tokens', label: 'Cache read ($/M)', flow: 'input' },
  { unit: 'cache_creation_input_tokens', label: 'Cache write ($/M)', flow: 'input' },
  { unit: 'input_images', label: 'Per image ($/M images)', flow: 'input', embeddingOnly: true },
];

interface FormState {
  model_name: string;
  litellm_model: string;
  provider: string;
  aws_region_name: string;
  mode: 'chat' | 'embedding';
  input_modes: string[];
  prices: Record<string, string>; // unit -> price string
}

const EMPTY_FORM: FormState = {
  model_name: '',
  litellm_model: '',
  provider: '',
  aws_region_name: '',
  mode: 'chat',
  input_modes: ['text', 'image'],
  prices: {},
};

const CATALOG_LIMIT = 50; // cap the rendered match list; the rest surface as you keep typing

// Which default roles a model can hold: chat models → chat; embedding models → text
// embedding, plus multimodal embedding when they accept images (graceful degradation).
function defaultRolesFor(m: GatewayModel): DefaultRole[] {
  if (m.mode === 'embedding') {
    return (m.input_modes ?? []).includes('image') ? ['embedding', 'multimodal_embedding'] : ['embedding'];
  }
  return ['chat'];
}

export function ModelGatewayPage() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [pickerOpen, setPickerOpen] = useState(false);
  // null = registering a new model; a gateway id = editing that model.
  const [editingId, setEditingId] = useState<string | null>(null);
  // Pending embedding-default switch awaiting confirmation (re-index implication).
  const [pendingDefault, setPendingDefault] = useState<{
    modelId: string;
    role: DefaultRole;
    modelName: string;
  } | null>(null);

  const { data: models = [], isLoading } = useQuery({
    queryKey: ['gateway-models'],
    queryFn: listGatewayModels,
  });

  // LiteLLM's known-model catalog, pre-filtered server-side to integrated providers.
  const { data: catalog = [] } = useQuery({
    queryKey: ['gateway-catalog'],
    queryFn: listModelCatalog,
  });

  // Picker matches: scoped to the chosen mode, substring-filtered on what's typed, capped.
  const q = form.litellm_model.trim().toLowerCase();
  const catalogMatches = catalog.filter(
    (c) => c.mode === form.mode && (q === '' || c.model_id.toLowerCase().includes(q)),
  );
  const visibleMatches = catalogMatches.slice(0, CATALOG_LIMIT);

  // Selecting a catalog model pre-fills the gateway id, provider, input modes and cost.
  const applyCatalogEntry = (entry: CatalogModel) => {
    const modes = ['text'];
    if (entry.supports_vision) modes.push('image');
    if (entry.supports_audio_input) modes.push('audio');
    if (entry.supports_pdf_input) modes.push('file');
    const perM = (v?: number | null) => (v && v > 0 ? String(v * 1_000_000) : undefined);
    const prices: Record<string, string> = {};
    const map: Array<[string, string | undefined]> = [
      ['base_input_tokens', perM(entry.input_cost_per_token)],
      ['base_output_tokens', perM(entry.output_cost_per_token)],
      ['cache_read_input_tokens', perM(entry.cache_read_input_token_cost)],
      ['cache_creation_input_tokens', perM(entry.cache_creation_input_token_cost)],
    ];
    for (const [unit, val] of map) if (val) prices[unit] = val;
    const isEmbedding = entry.mode === 'embedding';
    setForm((f) => ({
      ...f,
      litellm_model: entry.model_id,
      provider: entry.provider ?? f.provider,
      mode: isEmbedding ? 'embedding' : 'chat',
      input_modes: isEmbedding ? ['text'] : modes,
      prices: { ...f.prices, ...prices },
    }));
  };

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['gateway-models'] });
    queryClient.invalidateQueries({ queryKey: ['available-models'] }); // refresh every picker
  };

  const closeDialog = () => {
    setDialogOpen(false);
    setEditingId(null);
    setForm(EMPTY_FORM);
  };

  const openCreate = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setDialogOpen(true);
  };

  const openEdit = async (m: GatewayModel) => {
    setEditingId(m.model_id ?? null);
    setForm({
      model_name: m.model_name,
      litellm_model: m.litellm_model ?? '',
      provider: m.provider ?? '',
      aws_region_name: '',
      mode: m.mode === 'embedding' ? 'embedding' : 'chat',
      input_modes: m.input_modes && m.input_modes.length ? m.input_modes : ['text', 'image'],
      prices: {},
    });
    setDialogOpen(true);
    // Best-effort: seed the current rates from the gateway so edits start from real numbers.
    try {
      const { pricing } = await getCostPrefill(m.model_name);
      const prices: Record<string, string> = {};
      for (const [unit, entry] of Object.entries(pricing)) prices[unit] = String(entry.price_per_million);
      setForm((f) => ({ ...f, prices }));
    } catch {
      /* no seed — admin enters rates */
    }
  };

  const registerMutation = useMutation({
    mutationFn: registerGatewayModel,
    onSuccess: (res) => {
      toast.success(`Registered ${res.model_name}`);
      closeDialog();
      invalidate();
    },
    onError: (e: unknown) => toast.error(`Registration failed: ${errMsg(e)}`),
  });

  const updateMutation = useMutation({
    mutationFn: ({ modelId, body }: { modelId: string; body: ModelRegistrationRequest }) =>
      updateGatewayModel(modelId, body),
    onSuccess: (res) => {
      toast.success(`Updated ${res.model_name}`);
      closeDialog();
      invalidate();
    },
    onError: (e: unknown) => toast.error(`Update failed: ${errMsg(e)}`),
  });

  const testMutation = useMutation({
    mutationFn: testGatewayModel,
    onSuccess: (_r, name) => toast.success(`Test call to ${name} succeeded`),
    onError: (e: unknown) => toast.error(`Test failed: ${errMsg(e)}`),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteGatewayModel,
    onSuccess: () => {
      toast.success('Model removed from gateway');
      invalidate();
    },
    onError: (e: unknown) => toast.error(`Delete failed: ${errMsg(e)}`),
  });

  const defaultMutation = useMutation({
    mutationFn: ({ modelId, role }: { modelId: string; role: DefaultRole }) =>
      setGatewayModelDefault(modelId, role),
    onSuccess: (_r, { role }) => {
      toast.success(`Set as default ${role.replace('_', ' ')} (apps pick it up within ~60s)`);
      invalidate();
    },
    onError: (e: unknown) => toast.error(`Set default failed: ${errMsg(e)}`),
  });

  const prefill = async () => {
    if (!form.model_name) return;
    try {
      const { pricing } = await getCostPrefill(form.model_name);
      const prices: Record<string, string> = {};
      for (const [unit, entry] of Object.entries(pricing)) prices[unit] = String(entry.price_per_million);
      setForm((f) => ({ ...f, prices: { ...f.prices, ...prices } }));
      toast.success('Pre-filled cost from the gateway');
    } catch {
      toast.info('Gateway has no cost for this model yet — enter rates manually');
    }
  };

  const submit = () => {
    if (!form.model_name || !form.litellm_model || !form.provider) {
      toast.error('Alias, gateway model id, and provider are required');
      return;
    }
    // Embeddings bill input only; chat bills input/output (+ optional cache).
    const units =
      form.mode === 'embedding'
        ? PRICING_UNITS.filter((u) => u.flow === 'input')
        : PRICING_UNITS.filter((u) => !u.embeddingOnly);
    const pricing: Record<string, RateCardPricingEntry> = {};
    for (const { unit, flow } of units) {
      const raw = form.prices[unit];
      if (raw && Number(raw) > 0) pricing[unit] = { price_per_million: Number(raw), flow_direction: flow };
    }
    if (Object.keys(pricing).length === 0) {
      toast.error('Set at least one price — a model must be billable before it can be used');
      return;
    }
    const litellm_params: Record<string, unknown> = { model: form.litellm_model, max_retries: 0 };
    if (form.aws_region_name) litellm_params.aws_region_name = form.aws_region_name;

    const body: ModelRegistrationRequest = {
      model_name: form.model_name,
      litellm_params,
      mode: form.mode,
      input_modes: form.input_modes,
      provider: form.provider,
      pricing,
    };
    if (editingId) updateMutation.mutate({ modelId: editingId, body });
    else registerMutation.mutate(body);
  };

  const saving = registerMutation.isPending || updateMutation.isPending;

  const toggleMode = (mode: string) =>
    setForm((f) => ({
      ...f,
      input_modes: f.input_modes.includes(mode)
        ? f.input_modes.filter((m) => m !== mode)
        : [...f.input_modes, mode],
    }));

  return (
    <div className="container mx-auto p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Model Gateway</h1>
          <p className="text-muted-foreground text-sm">
            Register models at runtime — no redeploy. Each model writes a Rate Card (billing) and a
            gateway deployment (routing).
            <br />
            Runtime-registered models are editable; <span className="inline-flex items-center gap-0.5"><Lock className="h-3 w-3" /> from-config</span> models are read-only (defined in the proxy config).
          </p>
        </div>
        <Button onClick={openCreate}>
          <Plus className="mr-2 h-4 w-4" /> Register model
        </Button>
      </div>

      {isLoading ? (
        <p className="text-muted-foreground">Loading…</p>
      ) : models.length === 0 ? (
        <p className="text-muted-foreground">No models registered yet.</p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {models.map((m: GatewayModel) => {
            const testing = testMutation.isPending && testMutation.variables === m.model_name;
            return (
              <Card
                key={m.model_id ?? m.model_name}
                className={m.db_model ? undefined : 'border-dashed bg-muted/30'}
              >
                <CardHeader className="pb-3">
                  <CardTitle className="flex items-center gap-2 text-base">
                    {m.db_model ? (
                      <Cpu className="h-4 w-4 shrink-0" />
                    ) : (
                      <Lock className="h-4 w-4 shrink-0 text-muted-foreground" />
                    )}
                    {m.model_name}
                  </CardTitle>
                  <CardDescription className="font-mono text-xs break-all">{m.litellm_model}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="flex flex-wrap items-center gap-1.5">
                    {m.provider && <Badge variant="secondary">{m.provider}</Badge>}
                    {m.mode && <Badge variant="secondary">{m.mode}</Badge>}
                    {!m.db_model && (
                      <Badge variant="outline">
                        <Lock className="mr-1 h-3 w-3" /> from config
                      </Badge>
                    )}
                    {(m.default_roles ?? []).map((role) => (
                      <Badge key={role}>
                        <Star className="mr-1 h-3 w-3" /> default {role.replace('_', ' ')}
                      </Badge>
                    ))}
                    {m.supports_reasoning && (
                      <Badge variant="outline">
                        <Brain className="mr-1 h-3 w-3" /> thinking
                      </Badge>
                    )}
                    {m.supports_vision && (
                      <Badge variant="outline">
                        <Eye className="mr-1 h-3 w-3" /> vision
                      </Badge>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2 border-t pt-3">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => testMutation.mutate(m.model_name)}
                      disabled={testing}
                    >
                      {testing ? (
                        <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                      ) : (
                        <FlaskConical className="mr-1 h-3 w-3" />
                      )}
                      Test
                    </Button>
                    {/* Defaults are stored in our DB, so any model (config or db) can be one. */}
                    {m.model_id &&
                      defaultRolesFor(m).map((role) => (
                        <Button
                          key={role}
                          size="sm"
                          variant="ghost"
                          disabled={defaultMutation.isPending || (m.default_roles ?? []).includes(role)}
                          onClick={() =>
                            role === 'chat'
                              ? defaultMutation.mutate({ modelId: m.model_id!, role })
                              : setPendingDefault({ modelId: m.model_id!, role, modelName: m.model_name })
                          }
                        >
                          <Star className="mr-1 h-3 w-3" />
                          {role === 'chat' ? 'Make default' : `Default ${role.replace('_embedding', '')}`}
                        </Button>
                      ))}
                    {/* Edit/Remove only for db-backed models — LiteLLM can't mutate config models. */}
                    {m.model_id && m.db_model && (
                      <Button size="sm" variant="ghost" onClick={() => openEdit(m)}>
                        <Pencil className="mr-1 h-3 w-3" /> Edit
                      </Button>
                    )}
                    {m.model_id && m.db_model && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          if (
                            confirm(
                              `Remove ${m.model_name} from the gateway? Its Rate Card is kept for historical billing.`,
                            )
                          )
                            deleteMutation.mutate(m.model_id!);
                        }}
                      >
                        <Trash2 className="mr-1 h-3 w-3" /> Remove
                      </Button>
                    )}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <Dialog open={dialogOpen} onOpenChange={(o) => (o ? setDialogOpen(true) : closeDialog())}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{editingId ? 'Edit model' : 'Register model'}</DialogTitle>
            <DialogDescription>
              {editingId
                ? 'Update routing, capabilities and cost. Pricing changes are written as a new Rate Card version (prior rates are kept for historical billing).'
                : 'Routing + capabilities go to the gateway; pricing is written to the Rate Card first (a model must be billable before it’s usable).'}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="grid gap-1.5">
              <Label>Alias (what apps request)</Label>
              <Input
                placeholder="claude-sonnet-4.6"
                value={form.model_name}
                disabled={!!editingId}
                onChange={(e) => setForm({ ...form, model_name: e.target.value })}
              />
            </div>
            <div className="grid gap-1.5">
              <Label>Gateway model id{catalog.length > 0 ? ` (${form.mode} models — type to filter)` : ''}</Label>
              <div className="relative">
                <Input
                  placeholder="bedrock/eu.anthropic.claude-sonnet-4-6"
                  value={form.litellm_model}
                  autoComplete="off"
                  onFocus={() => setPickerOpen(true)}
                  onBlur={() => setTimeout(() => setPickerOpen(false), 150)}
                  onChange={(e) => {
                    setPickerOpen(true);
                    const v = e.target.value;
                    const entry = catalog.find((c) => c.model_id === v);
                    if (entry) applyCatalogEntry(entry);
                    else setForm({ ...form, litellm_model: v });
                  }}
                />
                {pickerOpen && catalog.length > 0 && visibleMatches.length > 0 && (
                  <div className="absolute z-50 mt-1 max-h-64 w-full overflow-auto rounded-md border bg-popover p-1 shadow-md">
                    {visibleMatches.map((c) => (
                      <button
                        type="button"
                        key={c.model_id}
                        className="flex w-full flex-col items-start rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent hover:text-accent-foreground"
                        onMouseDown={(e) => {
                          e.preventDefault(); // keep focus / beat onBlur so the click registers
                          applyCatalogEntry(c);
                          setPickerOpen(false);
                        }}
                      >
                        <span className="font-mono text-xs">{c.model_id}</span>
                        <span className="text-muted-foreground text-[11px]">
                          {c.provider} · {c.mode}
                          {c.supports_vision ? ' · vision' : ''}
                          {c.supports_reasoning ? ' · thinking' : ''}
                        </span>
                      </button>
                    ))}
                    {catalogMatches.length > visibleMatches.length && (
                      <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
                        +{catalogMatches.length - visibleMatches.length} more — keep typing to narrow
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
            <div className="grid gap-1.5">
              <Label>Mode</Label>
              <div className="flex gap-2">
                {(['chat', 'embedding'] as const).map((mode) => (
                  <Badge
                    key={mode}
                    variant={form.mode === mode ? 'default' : 'outline'}
                    className="cursor-pointer"
                    onClick={() =>
                      setForm((f) => ({
                        ...f,
                        mode,
                        input_modes: mode === 'embedding' ? ['text'] : f.input_modes,
                      }))
                    }
                  >
                    {mode}
                  </Badge>
                ))}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="grid gap-1.5">
                <Label>Provider (rate-card key)</Label>
                <Input
                  placeholder="bedrock"
                  value={form.provider}
                  onChange={(e) => setForm({ ...form, provider: e.target.value })}
                />
              </div>
              <div className="grid gap-1.5">
                <Label>AWS region (optional)</Label>
                <Input
                  placeholder="eu-central-1"
                  value={form.aws_region_name}
                  onChange={(e) => setForm({ ...form, aws_region_name: e.target.value })}
                />
              </div>
            </div>

            {form.mode === 'chat' && (
              <div className="grid gap-1.5">
                <Label>Input modes</Label>
                <div className="flex flex-wrap gap-2">
                  {ALL_INPUT_MODES.map((mode) => (
                    <Badge
                      key={mode}
                      variant={form.input_modes.includes(mode) ? 'default' : 'outline'}
                      className="cursor-pointer"
                      onClick={() => toggleMode(mode)}
                    >
                      {mode}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            <div className="grid gap-1.5">
              <div className="flex items-center justify-between">
                <Label>Pricing ($ per million units)</Label>
                <Button type="button" size="sm" variant="ghost" onClick={prefill}>
                  Pre-fill from gateway
                </Button>
              </div>
              <div className="grid grid-cols-2 gap-2">
                {(form.mode === 'embedding'
                  ? PRICING_UNITS.filter((u) => u.flow === 'input')
                  : PRICING_UNITS.filter((u) => !u.embeddingOnly)
                ).map(({ unit, label }) => (
                  <div key={unit} className="grid gap-1">
                    <Label className="text-xs text-muted-foreground">{label}</Label>
                    <Input
                      type="number"
                      step="0.0001"
                      value={form.prices[unit] ?? ''}
                      onChange={(e) => setForm({ ...form, prices: { ...form.prices, [unit]: e.target.value } })}
                    />
                  </div>
                ))}
              </div>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={closeDialog}>
              Cancel
            </Button>
            <Button onClick={submit} disabled={saving}>
              {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {editingId
                ? saving
                  ? 'Saving…'
                  : 'Save changes'
                : saving
                  ? 'Registering…'
                  : 'Register'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Switching an embedding default re-points indexing — warn about re-indexing. */}
      <AlertDialog open={!!pendingDefault} onOpenChange={(o) => !o && setPendingDefault(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Change the default {pendingDefault?.role.replace('_', ' ')} model?</AlertDialogTitle>
            <AlertDialogDescription>
              <strong>{pendingDefault?.modelName}</strong> will become the default{' '}
              {pendingDefault?.role.replace('_', ' ')} model. Existing catalogs and document stores were
              indexed with the current model — their vectors come from a different model and won’t be
              directly comparable. New content will embed with the new model; for consistent search you
              should <strong>re-index existing catalogs</strong> after switching. This does not affect
              already-stored vectors until you re-index.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingDefault)
                  defaultMutation.mutate({ modelId: pendingDefault.modelId, role: pendingDefault.role });
                setPendingDefault(null);
              }}
            >
              Switch &amp; require re-index
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
