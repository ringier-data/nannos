import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Plus,
  Trash2,
  FlaskConical,
  Cpu,
  Eye,
  Brain,
  Star,
  Pencil,
  Loader2,
  Lock,
  ChevronDown,
} from 'lucide-react';
import { toast } from 'sonner';

import {
  getGatewayConfig,
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
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
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
  vertex_location: string;
  vertex_project: string;
  base_model: string; // Azure only: maps a deployment name to a known model for cost/metadata
  mode: 'chat' | 'embedding';
  input_modes: string[];
  prices: Record<string, string>; // unit -> price string
}

const EMPTY_FORM: FormState = {
  model_name: '',
  litellm_model: '',
  provider: '',
  aws_region_name: '',
  vertex_location: '',
  vertex_project: '',
  base_model: '',
  mode: 'chat',
  input_modes: ['text', 'image'],
  prices: {},
};

// Credential fields are provider-specific: Bedrock takes an AWS region, Vertex AI takes
// vertex_project/vertex_location. Other providers (azure, gemini, …) take neither here.
const isVertexProvider = (provider: string) => provider.startsWith('vertex_ai');
const isBedrockProvider = (provider: string) => provider.startsWith('bedrock');
// Azure deployment names are arbitrary and not in LiteLLM's cost map, so cost tracking +
// max-tokens metadata need a base_model mapping to a known model (e.g. azure/gpt-4o).
const isAzureProvider = (provider: string) => provider.startsWith('azure');

// Region/account/vendor qualifiers we strip when suggesting an alias from a model id.
const ALIAS_QUALIFIERS =
  /^(eu|us|apac|global|anthropic|amazon|meta|cohere|mistral|google|ai21|deepseek|qwen|stability|writer|luma|twelvelabs)$/i;

// Suggest a request alias from a gateway model id: drop the provider prefix and any
// leading region/vendor qualifiers, e.g. "bedrock/eu.anthropic.claude-sonnet-4-6" → "claude-sonnet-4-6".
function deriveAlias(modelId: string): string {
  const tail = modelId.includes('/') ? modelId.slice(modelId.lastIndexOf('/') + 1) : modelId;
  const parts = tail.split('.');
  while (parts.length > 1 && ALIAS_QUALIFIERS.test(parts[0])) parts.shift();
  return parts.join('.');
}

// The litellm provider family is the gateway model id prefix (the part before the first "/"),
// e.g. "vertex_ai/gemini-embedding-2" → "vertex_ai", "bedrock/eu.anthropic.claude-…" → "bedrock".
// This is exactly how the cost logger resolves provider for billing (custom_llm_provider, else
// the deployment-id prefix), so deriving it here keeps the rate card keyed on the same provider
// usage is logged under. Critically, it prevents a region/location (Vertex "eu"/"global") from
// being typed into the free-text provider field and silently mis-keying billing to $0.
// Empty when the id has no prefix (e.g. a bare Azure deployment name) — admin sets it then.
function deriveProvider(modelId: string): string {
  return modelId.includes('/') ? modelId.slice(0, modelId.indexOf('/')) : '';
}

const CATALOG_LIMIT = 50; // cap the rendered match list; the rest surface as you keep typing

// Which default roles a model can hold: chat models → the standard chat default plus the
// low/premium capability tiers (sub-agents bind to a tier; the slot picks the model);
// embedding models → text embedding, plus multimodal embedding when they accept images.
function defaultRolesFor(m: GatewayModel): DefaultRole[] {
  if (m.mode === 'embedding') {
    return (m.input_modes ?? []).includes('image') ? ['embedding', 'multimodal_embedding'] : ['embedding'];
  }
  return ['chat', 'chat:low', 'chat:premium'];
}

// An embedding model accepts images when LiteLLM lists a per-image input cost — the one
// signal set across providers (Gemini, Vertex multimodalembedding, Bedrock Nova/Titan), even
// where supports_vision/supported_modalities are absent. Drives the 'image' input mode, which
// in turn unlocks the multimodal_embedding default (see defaultRolesFor).
const embeddingInputModes = (entry?: CatalogModel): string[] =>
  entry && (entry.input_cost_per_image ?? 0) > 0 ? ['text', 'image'] : ['text'];

// Embedding-role switches trigger a re-index, so they go through a confirmation dialog;
// chat/tier defaults apply immediately.
const isEmbeddingRole = (role: DefaultRole): boolean => role === 'embedding' || role === 'multimodal_embedding';

// Per-million price (advisory — helps decide which model to assign to a tier; rate cards
// remain the billing source of truth). Gateway costs are per-token.
const perMillion = (v?: number | null): string | null =>
  v && v > 0 ? `$${(v * 1_000_000).toFixed(2)}/M` : null;

// Human label for a default role / tier slot. Accepts a plain string because the gateway
// types `default_roles` loosely; unknown roles fall through to their raw value.
const roleLabel = (role: string): string =>
  (({
    chat: 'chat',
    'chat:low': 'low tier',
    'chat:premium': 'premium tier',
    embedding: 'embedding',
    multimodal_embedding: 'multimodal embedding',
  }) as Record<string, string>)[role] ?? role;

export function ModelGatewayPage() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [pickerOpen, setPickerOpen] = useState(false);
  // Provider credential overrides (region/project) are hidden by default — the gateway's
  // env defaults are the norm; only collapse-open them when overriding per model.
  const [credsOpen, setCredsOpen] = useState(false);
  // Once the alias is hand-edited, stop auto-filling it from the picked model.
  const [aliasEdited, setAliasEdited] = useState(false);
  // null = registering a new model; a gateway id = editing that model.
  const [editingId, setEditingId] = useState<string | null>(null);
  // Pending embedding-default switch awaiting confirmation (re-index implication).
  const [pendingDefault, setPendingDefault] = useState<{
    modelId: string;
    role: DefaultRole;
    modelName: string;
  } | null>(null);
  // Model pending removal, awaiting confirmation (shared ConfirmDialog, not a native confirm()).
  const [pendingDelete, setPendingDelete] = useState<GatewayModel | null>(null);

  const { data: models = [], isLoading } = useQuery({
    queryKey: ['gateway-models'],
    queryFn: listGatewayModels,
  });

  // LiteLLM's known-model catalog, pre-filtered server-side to integrated providers.
  const { data: catalog = [] } = useQuery({
    queryKey: ['gateway-catalog'],
    queryFn: listModelCatalog,
  });

  // Deployment defaults (env-driven). The Vertex serving region the proxy falls back to — shown
  // as the location placeholder so the admin isn't nudged toward a wrong region.
  const { data: gatewayConfig } = useQuery({
    queryKey: ['gateway-config'],
    queryFn: getGatewayConfig,
  });
  const defaultVertexLocation = gatewayConfig?.default_vertex_location || 'eu';
  // Deployment project id (env-driven) as a placeholder hint — never a hardcoded project.
  const defaultVertexProject = gatewayConfig?.default_vertex_project || 'my-gcp-project';

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
      ['input_images', perM(entry.input_cost_per_image)],
    ];
    for (const [unit, val] of map) if (val) prices[unit] = val;
    const isEmbedding = entry.mode === 'embedding';
    setForm((f) => ({
      ...f,
      litellm_model: entry.model_id,
      // Pre-fill the alias from the model unless the user has already typed their own.
      model_name: !editingId && !aliasEdited ? deriveAlias(entry.model_id) : f.model_name,
      provider: entry.provider ?? f.provider,
      mode: isEmbedding ? 'embedding' : 'chat',
      input_modes: isEmbedding ? embeddingInputModes(entry) : modes,
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
    setCredsOpen(false);
    setAliasEdited(false);
    setDialogOpen(true);
  };

  const openEdit = async (m: GatewayModel) => {
    setEditingId(m.model_id ?? null);
    const awsRegion = m.aws_region_name ?? '';
    const vertexLocation = m.vertex_location ?? '';
    const vertexProject = m.vertex_project ?? '';
    // Expand the Advanced section up front when the model carries routing params, so the admin
    // sees the values that will round-trip (they're hidden behind the collapsible otherwise).
    setCredsOpen(Boolean(awsRegion || vertexLocation || vertexProject));
    setAliasEdited(true); // existing alias is fixed (input is disabled on edit)
    setForm({
      model_name: m.model_name,
      litellm_model: m.litellm_model ?? '',
      provider: m.provider ?? '',
      aws_region_name: awsRegion,
      vertex_location: vertexLocation,
      vertex_project: vertexProject,
      base_model: m.base_model ?? '',
      mode: m.mode === 'embedding' ? 'embedding' : 'chat',
      input_modes: m.input_modes && m.input_modes.length ? m.input_modes : ['text', 'image'],
      prices: {},
    });
    setDialogOpen(true);
    // Best-effort: seed the current rates from the gateway so edits start from real numbers.
    try {
      const { pricing } = await getCostPrefill(m.model_name);
      const prices: Record<string, string> = {};
      for (const [unit, entry] of Object.entries(pricing ?? {})) prices[unit] = String(entry.price_per_million);
      setForm((f) => ({ ...f, prices }));
    } catch {
      /* no seed — admin enters rates */
    }
  };

  // Save = persist, then validate with a live ping before we consider the model usable.
  // A newly-registered model that fails the test is deleted again, so a failed save never
  // leaves a broken alias behind. Edits apply first and aren't rolled back (we hold no
  // snapshot of the prior params) — the admin is told the change landed but failed its test.
  const saveMutation = useMutation({
    mutationFn: async (body: ModelRegistrationRequest) => {
      if (editingId) {
        await updateGatewayModel(editingId, body);
        await testGatewayModel(body.model_name); // throws on a failed ping
        return { name: body.model_name, created: null as GatewayModel | null };
      }
      const res = await registerGatewayModel(body);
      try {
        await testGatewayModel(res.model_name); // throws on a failed ping
      } catch (testErr) {
        if (res.gateway_model_id) {
          // Best-effort rollback; surface the original test error regardless of cleanup outcome.
          await deleteGatewayModel(res.gateway_model_id).catch(() => {});
        }
        throw testErr;
      }
      // Build the card from what we just submitted so the page can reflect the write
      // immediately — a refetch here is unreliable (see onSuccess).
      const created: GatewayModel = {
        model_name: res.model_name,
        model_id: res.gateway_model_id ?? null,
        provider: body.provider,
        litellm_model: (body.litellm_params.model as string | undefined) ?? null,
        mode: body.mode ?? 'chat',
        input_modes: body.input_modes,
        default_roles: [],
        db_model: true,
        supports_vision: (body.input_modes ?? []).includes('image'),
      };
      // First model to serve a role becomes the fleet default automatically, so a fresh
      // system always has a fallback without a separate "Make default" click. Only fill
      // roles that nothing already holds (config or db model) — never steal an existing
      // default. Capability tiers are EXCLUDED from auto-assignment: which model is the
      // low/premium tier is an explicit admin decision, not something a new model silently
      // grabs. Best-effort: a failed set must not roll back the good registration, and it
      // runs after the test so we never default an alias we're about to delete.
      const autoRoles = defaultRolesFor(created).filter(
        (role) => role !== 'chat:low' && role !== 'chat:premium',
      ).filter(
        (role) => !models.some((m) => (m.default_roles ?? []).includes(role)),
      );
      if (res.gateway_model_id && autoRoles.length) {
        for (const role of autoRoles) {
          await setGatewayModelDefault(res.gateway_model_id, role).catch(() => {});
        }
        created.default_roles = autoRoles;
      }
      return { name: res.model_name, created };
    },
    onSuccess: ({ name, created }) => {
      const auto = created?.default_roles ?? [];
      toast.success(
        auto.length
          ? `Saved & tested ${name} — set as default ${auto.map((r) => r.replace('_', ' ')).join(' & ')}`
          : `Saved & tested ${name}`,
      );
      closeDialog();
      if (created) {
        // The gateway runs multiple replicas and serves /model/info from per-pod memory,
        // so an immediate refetch usually lands on a replica that hasn't picked up the new
        // model yet (it propagates on each pod's DB reload). Insert it optimistically so the
        // page reflects the write right away; the next natural refetch reconciles once the
        // gateway propagates. We deliberately don't invalidate ['gateway-models'] here —
        // that would refetch the still-stale list and wipe this card.
        queryClient.setQueryData<GatewayModel[]>(['gateway-models'], (old = []) =>
          old.some((m) => m.model_name === created.model_name) ? old : [...old, created],
        );
        queryClient.invalidateQueries({ queryKey: ['available-models'] }); // refresh every picker
      } else {
        invalidate(); // edit landed in place — reflect the gateway's real state
      }
    },
    onError: (e: unknown) => {
      toast.error(
        editingId
          ? `Update applied but its test failed — please verify: ${errMsg(e)}`
          : `Test failed — registration rolled back: ${errMsg(e)}`,
      );
      invalidate(); // an edit may have landed; reflect the gateway's real state
    },
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
      for (const [unit, entry] of Object.entries(pricing ?? {})) prices[unit] = String(entry.price_per_million);
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
    // The rate card MUST be keyed on the same provider usage is billed under. The cost logger
    // resolves provider from the model-id prefix, so when the id has one we take it as
    // authoritative — overriding whatever is in the free-text field. This is what stops a Vertex
    // location ("eu"/"global") in the provider field from creating an orphan rate card that never
    // matches usage (→ silent $0 billing).
    const provider = deriveProvider(form.litellm_model) || form.provider;
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
    if (isVertexProvider(provider)) {
      if (form.vertex_location) litellm_params.vertex_location = form.vertex_location;
      if (form.vertex_project) litellm_params.vertex_project = form.vertex_project;
    } else if (isBedrockProvider(provider) && form.aws_region_name) {
      litellm_params.aws_region_name = form.aws_region_name;
    }

    // base_model only matters when the routed model id isn't a known model (Azure deployments).
    const model_info: Record<string, unknown> = {};
    if (isAzureProvider(provider) && form.base_model.trim()) {
      model_info.base_model = form.base_model.trim();
    }

    const body: ModelRegistrationRequest = {
      model_name: form.model_name,
      litellm_params,
      ...(Object.keys(model_info).length ? { model_info } : {}),
      mode: form.mode,
      input_modes: form.input_modes,
      provider,
      pricing,
    };
    saveMutation.mutate(body);
  };

  const saving = saveMutation.isPending;

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
                  {(perMillion(m.input_cost_per_token) || perMillion(m.output_cost_per_token)) && (
                    <CardDescription className="text-xs">
                      {perMillion(m.input_cost_per_token) && <span>in {perMillion(m.input_cost_per_token)}</span>}
                      {perMillion(m.input_cost_per_token) && perMillion(m.output_cost_per_token) && <span> · </span>}
                      {perMillion(m.output_cost_per_token) && <span>out {perMillion(m.output_cost_per_token)}</span>}
                    </CardDescription>
                  )}
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
                        <Star className="mr-1 h-3 w-3" /> default {roleLabel(role)}
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
                            isEmbeddingRole(role)
                              ? setPendingDefault({ modelId: m.model_id!, role, modelName: m.model_name })
                              : defaultMutation.mutate({ modelId: m.model_id!, role })
                          }
                        >
                          <Star className="mr-1 h-3 w-3" />
                          {role === 'chat' ? 'Make default' : `Default ${roleLabel(role)}`}
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
                        onClick={() => setPendingDelete(m)}
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
                        input_modes:
                          mode === 'embedding'
                            ? embeddingInputModes(catalog.find((c) => c.model_id === f.litellm_model))
                            : f.input_modes,
                      }))
                    }
                  >
                    {mode}
                  </Badge>
                ))}
              </div>
            </div>
            <div className="grid gap-1.5">
              <Label>Gateway model id{catalog.length > 0 ? ` (${form.mode} models — type to filter)` : ''}</Label>
              <div className="relative">
                <Input
                  placeholder="bedrock/eu.anthropic.claude-sonnet-4-6"
                  value={form.litellm_model}
                  autoComplete="off"
                  onBlur={() => setTimeout(() => setPickerOpen(false), 150)}
                  onChange={(e) => {
                    setPickerOpen(true);
                    const v = e.target.value;
                    const entry = catalog.find((c) => c.model_id === v);
                    if (entry) applyCatalogEntry(entry);
                    // No catalog match (e.g. local dev with an empty catalog): still derive the
                    // provider from the id prefix so it stays correct without manual entry — a
                    // region typed here is what mis-keyed billing before.
                    else setForm({ ...form, litellm_model: v, provider: deriveProvider(v) || form.provider });
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
              <Label>Alias (what apps request)</Label>
              <Input
                placeholder="claude-sonnet-4.6"
                value={form.model_name}
                disabled={!!editingId}
                onChange={(e) => {
                  setAliasEdited(true);
                  setForm({ ...form, model_name: e.target.value });
                }}
              />
              {!editingId && (
                <p className="text-[11px] text-muted-foreground">
                  Auto-filled from the model id — edit to set a custom alias.
                </p>
              )}
            </div>

            <div className="grid gap-1.5">
              <Label>Provider (rate-card key)</Label>
              <Input
                placeholder="bedrock"
                value={form.provider}
                onChange={(e) => setForm({ ...form, provider: e.target.value })}
              />
            </div>

            {isAzureProvider(form.provider) && (
              <div className="grid gap-1.5">
                <Label>Base model (Azure)</Label>
                <Input
                  placeholder="azure/gpt-4o"
                  value={form.base_model}
                  onChange={(e) => setForm({ ...form, base_model: e.target.value })}
                />
                <p className="text-[11px] text-muted-foreground">
                  Azure deployment names aren’t recognised for cost/metadata. Map this deployment to a
                  known model (e.g. <span className="font-mono">azure/gpt-4o</span>) so the gateway can
                  identify it for max-tokens and native cost tracking.
                </p>
              </div>
            )}

            {(isVertexProvider(form.provider) || isBedrockProvider(form.provider)) && (
              <Collapsible open={credsOpen} onOpenChange={setCredsOpen}>
                <CollapsibleTrigger className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors [&[data-state=open]>svg]:rotate-180">
                  <ChevronDown className="h-4 w-4 transition-transform" />
                  Advanced — region & credentials
                </CollapsibleTrigger>
                <CollapsibleContent className="grid gap-3 pt-3">
                  {isBedrockProvider(form.provider) && (
                    <div className="grid gap-1.5">
                      <Label>AWS region (optional)</Label>
                      <Input
                        placeholder="eu-central-1"
                        value={form.aws_region_name}
                        onChange={(e) => setForm({ ...form, aws_region_name: e.target.value })}
                      />
                    </div>
                  )}
                  {isVertexProvider(form.provider) && (
                    <div className="grid grid-cols-2 gap-3">
                      <div className="grid gap-1.5">
                        <Label>Vertex location (optional)</Label>
                        <Input
                          placeholder={defaultVertexLocation}
                          value={form.vertex_location}
                          onChange={(e) => setForm({ ...form, vertex_location: e.target.value })}
                        />
                        <p className="text-[11px] text-muted-foreground">
                          Serving region, not the GCP project. Leave blank to use the deployment
                          default ({defaultVertexLocation}). Some models (e.g. Gemini embeddings) 404
                          outside it.
                        </p>
                      </div>
                      <div className="grid gap-1.5">
                        <Label>Vertex project (optional)</Label>
                        <Input
                          placeholder={defaultVertexProject}
                          value={form.vertex_project}
                          onChange={(e) => setForm({ ...form, vertex_project: e.target.value })}
                        />
                        <p className="text-[11px] text-muted-foreground">
                          GCP project id. Leave blank to use the proxy's default project.
                        </p>
                      </div>
                    </div>
                  )}
                </CollapsibleContent>
              </Collapsible>
            )}

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
                  ? 'Saving & testing…'
                  : 'Save changes'
                : saving
                  ? 'Registering & testing…'
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

      {/* Remove a registered model — shared ConfirmDialog (consistent with other admin pages). */}
      <ConfirmDialog
        open={!!pendingDelete}
        onOpenChange={(o) => !o && setPendingDelete(null)}
        title={`Remove ${pendingDelete?.model_name ?? 'model'}?`}
        description="This removes the model from the gateway. Its Rate Card is kept for historical billing."
        confirmLabel="Remove"
        variant="destructive"
        isLoading={deleteMutation.isPending}
        onConfirm={() => {
          if (pendingDelete?.model_id) deleteMutation.mutate(pendingDelete.model_id);
          setPendingDelete(null);
        }}
      />
    </div>
  );
}
