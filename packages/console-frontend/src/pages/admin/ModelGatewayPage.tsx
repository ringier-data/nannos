import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Plus,
  Trash2,
  FlaskConical,
  Cpu,
  Eye,
  Brain,
  Globe,
  Star,
  Pencil,
  Loader2,
  Lock,
  ChevronDown,
  AlertTriangle,
} from 'lucide-react';
import { toast } from 'sonner';

import {
  getBedrockRegions,
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
import { ProviderMismatchBanner } from '@/components/admin/ProviderMismatchBanner';
import { PROVIDER_CONFIG_QUERY_KEY } from '@/lib/providerCheckQuery';
import { WebSearchSettings } from '@/components/admin/WebSearchSettings';
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

// Bedrock rejects a model that isn't offered in the region it was called in with this message, and
// says nothing about the region — which reads as "this model can't be registered" when it is really
// "wrong region". Recognizing it lets the dialog name the region and point at the region field.
// (Verified 2026-08-05: amazon.nova-2-multimodal-embeddings-v1:0 is absent from eu-central-1 and
// works in us-east-1, sync embeddings included.)
const BEDROCK_WRONG_REGION = /provided model identifier is invalid/i;

function bedrockRegionHint(
  message: string,
  modelId: string,
  region: string,
  availableIn: string[] | null,
): string | null {
  if (!BEDROCK_WRONG_REGION.test(message)) return null;
  const where = region ? `region ${region}` : "the region it was called in";
  const remedy = availableIn?.length
    ? `AWS offers it in ${availableIn.join(', ')} — set one of those as the AWS region under ` +
      '“Advanced — region & credentials” and register again.'
    : 'This is a region problem, not a bad model id: set the AWS region under “Advanced — region & ' +
      'credentials” (e.g. us-east-1 for the Nova multimodal embedding models) and register again.';
  return `AWS doesn't offer ${modelId || 'this model'} in ${where}. ${remedy}`;
}

// billing_unit -> flow_direction. These match the units the proxy CustomLogger emits.
// `embeddingOnly` units only apply to embedding models (e.g. multimodal image inputs).
const PRICING_UNITS: Array<{
  unit: string;
  label: string;
  flow: RateCardPricingEntry['flow_direction'];
  embeddingOnly?: boolean;
  webSearchOnly?: boolean;
}> = [
  { unit: 'base_input_tokens', label: 'Input ($/M tokens)', flow: 'input' },
  { unit: 'base_output_tokens', label: 'Output ($/M tokens)', flow: 'output' },
  { unit: 'cache_read_input_tokens', label: 'Cache read ($/M)', flow: 'input' },
  { unit: 'cache_creation_input_tokens', label: 'Cache write ($/M)', flow: 'input' },
  { unit: 'input_images', label: 'Per image ($/M images)', flow: 'input', embeddingOnly: true },
  // Per-grounded-call web-search fee (matches the proxy's `web_search` billing unit). Only shown
  // for web-search-capable models — i.e. once the gateway prefill reports a price for it — so it
  // isn't a confusing empty field on chat models that can't search. See webSearchOnly gating below.
  { unit: 'web_search', label: 'Web search ($/M searches)', flow: 'output', webSearchOnly: true },
];

// The pricing fields shown/submitted for a given mode. web_search is gated on the model being
// able to search — the capability toggle, or a prefill having surfaced a fee (capable models
// only); everything else follows the input/embedding split.
const visiblePricingUnits = (mode: string, prices: Record<string, string>, canSearch = false) =>
  (mode === 'embedding'
    ? PRICING_UNITS.filter((u) => u.flow === 'input')
    : PRICING_UNITS.filter((u) => !u.embeddingOnly)
  ).filter((u) => !u.webSearchOnly || canSearch || prices[u.unit] != null);

interface FormState {
  model_name: string;
  litellm_model: string;
  // Provider ROUTE, never authored here and never sent: seeded from the picked catalog entry's
  // server-resolved `family` (or, on edit, from the gateway) purely so the UI knows which
  // credential inputs this route takes. `effectiveProvider` prefers the model id / catalog.
  provider: string;
  aws_region_name: string;
  vertex_location: string;
  vertex_project: string;
  base_model: string; // Azure only: maps a deployment name to a known model for cost/metadata
  mode: 'chat' | 'embedding';
  input_modes: string[];
  // Capability toggles, stored as EXPLICIT booleans in the deployment's model_info. Stored keys
  // shadow LiteLLM's cost map (its /model/info merge only fills keys the deployment doesn't set),
  // so these stay editable for models the catalog doesn't know yet — the reason they exist.
  supports_reasoning: boolean;
  supports_web_search: boolean;
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
  supports_reasoning: false,
  supports_web_search: false,
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

// The provider route is the gateway model id prefix (the part before the first "/"), e.g.
// "vertex_ai/gemini-embedding-2" → "vertex_ai". That prefix is how LiteLLM routes the call AND how
// the cost logger keys billing (custom_llm_provider, else the deployment-id prefix), which is why a
// single value covers routing, provider-specific params and the rate card. Read-only mirror of the
// server's own resolution — the display only, never a submitted value.
// Empty when the id has no prefix (the norm for Bedrock cost-map ids): the catalog entry's
// server-resolved `family` answers those, and the server re-derives it the same way on save.
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
    search: 'search',
  }) as Record<string, string>)[role] ?? role;

export function ModelGatewayPage() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [pickerOpen, setPickerOpen] = useState(false);
  // Provider credential overrides (region/project) are hidden by default — the gateway's
  // env defaults are the norm; only collapse-open them when overriding per model.
  const [credsOpen, setCredsOpen] = useState(false);
  // Sticky, in-dialog explanation for the one failure a toast handles badly: AWS rejecting a model
  // that simply isn't in the region. The dialog stays open on failure, so the fix (the region field
  // right above) and the reason must both be on screen — a toast is gone before the admin reads it.
  const [regionError, setRegionError] = useState<string | null>(null);
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
  // The region a Bedrock model with a blank region is actually called in. Named in the UI because
  // Bedrock availability is regional and AWS's rejection doesn't say which region it checked.
  const defaultBedrockRegion = gatewayConfig?.default_bedrock_region || '';

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
    // Web search is a per-query fee keyed by context size; price the `medium` tier (what
    // gateway_web_search sends), falling back to low/high — mirrors the backend cost-prefill.
    const search = entry.search_context_cost_per_query;
    const perQuery =
      search?.search_context_size_medium ??
      search?.search_context_size_low ??
      search?.search_context_size_high;
    const map: Array<[string, string | undefined]> = [
      ['base_input_tokens', perM(entry.input_cost_per_token)],
      ['base_output_tokens', perM(entry.output_cost_per_token)],
      ['cache_read_input_tokens', perM(entry.cache_read_input_token_cost)],
      ['cache_creation_input_tokens', perM(entry.cache_creation_input_token_cost)],
      ['input_images', perM(entry.input_cost_per_image)],
      ['web_search', perM(perQuery)],
    ];
    for (const [unit, val] of map) if (val) prices[unit] = val;
    const isEmbedding = entry.mode === 'embedding';
    setForm((f) => ({
      ...f,
      litellm_model: entry.model_id,
      // Pre-fill the alias from the model unless the user has already typed their own.
      model_name: !editingId && !aliasEdited ? deriveAlias(entry.model_id) : f.model_name,
      // The server-resolved route, not LiteLLM's cost-map tag — this only drives which
      // credential inputs show; the request carries no provider (see effectiveProvider).
      provider: entry.family ?? f.provider,
      mode: isEmbedding ? 'embedding' : 'chat',
      input_modes: isEmbedding ? embeddingInputModes(entry) : modes,
      // Capabilities from the catalog entry; a listed per-query search fee also counts as
      // "can search" (some entries carry the fee without the boolean). Editable after.
      supports_reasoning: !isEmbedding && !!entry.supports_reasoning,
      supports_web_search: !isEmbedding && (!!entry.supports_web_search || !!perQuery),
      // Replace (not merge): selecting a different model must not leave a prior model's prices —
      // e.g. a stale web_search fee on a model that can't search, or stale cache rates.
      prices,
    }));
  };

  // The provider route this deployment will be served and billed under. ONE value answers all of it,
  // and the form never authors it — it mirrors the server's resolution so what you see is what will
  // be written: the model id's own route prefix, else the route of that id's catalog entry
  // (`family`, derived server-side — the norm for Bedrock, whose cost-map ids are bare). The trailing
  // form.provider is only a fallback for an already-registered model whose id is unprefixed and
  // absent from the catalog; it is seeded from the gateway, never typed. Nothing here is sent —
  // registration carries no provider field at all.
  const derivedProvider = deriveProvider(form.litellm_model);
  // A route the SERVER would also resolve — the id's prefix or the catalog entry's server-derived
  // family. Everything the UI promises about saving must be based on this, never on the wider
  // `effectiveProvider` below, whose form.provider tail can be a cost-map TAG (`bedrock_converse`,
  // seeded from the gateway on edit) that nothing routes and registration 422s.
  const routableProvider =
    derivedProvider || (catalog.find((c) => c.model_id === form.litellm_model)?.family ?? '');
  // Adds that tag tail: still useful for deciding WHICH credential fields a provider takes (a
  // `bedrock_converse` model is a Bedrock model), but never for what will be written.
  const effectiveProvider = routableProvider || form.provider;

  // The region THIS deployment will be called in: its own pin, else the gateway's.
  const effectiveBedrockRegion = form.aws_region_name.trim() || defaultBedrockRegion;

  // Which regions offer the chosen Bedrock id. Only asked once the id is a real catalog entry (the
  // picker's normal path): probing per keystroke would be a pointless AWS call per character, and a
  // half-typed id has no answer. Long-cached server-side; advisory, so failures stay invisible.
  const bedrockModelId = form.litellm_model.trim().replace(/^bedrock\//, '');
  const isKnownCatalogId = catalog.some((c) => c.model_id === bedrockModelId);
  const { data: bedrockRegions } = useQuery({
    queryKey: ['bedrock-regions', bedrockModelId],
    queryFn: () => getBedrockRegions(bedrockModelId),
    enabled: dialogOpen && isBedrockProvider(effectiveProvider) && isKnownCatalogId,
    staleTime: 60 * 60_000,
    retry: false,
  });
  // Four distinct states, and they must stay distinct: the model is in the region we'll call
  // ('here'), it exists but not there ('elsewhere' — the actionable one), no probed region has it
  // ('nowhere' — most likely a bad id), or we know the regions but not which one this deployment
  // will use ('unknown-region'), where saying "not offered here" would be a fabrication.
  const bedrockAvailability = !bedrockRegions?.regions
    ? null
    : bedrockRegions.regions.length === 0
      ? 'nowhere'
      : !effectiveBedrockRegion
        ? 'unknown-region'
        : bedrockRegions.regions.includes(effectiveBedrockRegion)
          ? 'here'
          : 'elsewhere';

  // An alias addresses exactly one deployment here (the server 409s on a duplicate): the rate card,
  // the role defaults and the provider check are all keyed on it, and Edit/Remove act on one gateway
  // id. Flag the collision while typing — picking the same catalog entry twice auto-fills the same
  // alias, which is the easy way to end up with two cards for one name.
  const aliasTaken = !editingId && models.some((m) => m.model_name === form.model_name.trim());

  // Registering, editing and deleting a model all change what the billing check sees (a register/edit
  // writes the correctly-keyed rate card; a delete removes the deployment it flags), so its cached
  // result must go — otherwise the banner keeps showing the mismatch the admin just fixed and its
  // Re-key button 409s. Kept callable on its own because the register path deliberately does NOT
  // refetch the model list (see saveMutation.onSuccess).
  const invalidateProviderCheck = () => {
    queryClient.invalidateQueries({ queryKey: PROVIDER_CONFIG_QUERY_KEY });
  };

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['gateway-models'] });
    queryClient.invalidateQueries({ queryKey: ['available-models'] }); // refresh every picker
    invalidateProviderCheck();
  };

  const closeDialog = () => {
    setDialogOpen(false);
    setEditingId(null);
    setForm(EMPTY_FORM);
    setRegionError(null);
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
      // Current effective flags (stored or catalog-merged); saving writes them back explicitly.
      supports_reasoning: !!m.supports_reasoning,
      supports_web_search: !!m.supports_web_search,
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
        // The key the server actually wrote the card under: the request carries no provider at all
        // (the server derives it), and an unprefixed catalog id submitted with its catalog tag
        // (`bedrock_converse`) is stored as the family (`bedrock`) — only the response knows.
        provider: res.provider,
        litellm_model: (body.litellm_params.model as string | undefined) ?? null,
        mode: body.mode ?? 'chat',
        input_modes: body.input_modes,
        default_roles: [],
        db_model: true,
        supports_vision: (body.input_modes ?? []).includes('image'),
        supports_reasoning: (body.model_info?.supports_reasoning as boolean | undefined) ?? false,
        supports_web_search: (body.model_info?.supports_web_search as boolean | undefined) ?? false,
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
        invalidateProviderCheck(); // the new model's rate card just landed — re-run the banner check
      } else {
        invalidate(); // edit landed in place — reflect the gateway's real state
      }
    },
    onError: (e: unknown) => {
      const message = errMsg(e);
      // Bedrock's "invalid model identifier" is a region verdict in disguise. Keep it in the dialog,
      // next to the field that fixes it, and open that section so it's visible without a click.
      const hint = bedrockRegionHint(
        message,
        form.litellm_model,
        effectiveBedrockRegion,
        // If the availability probe answered, name the regions that DO have it instead of leaving
        // the admin to guess which one to type.
        bedrockRegions?.regions ?? null,
      );
      if (hint) {
        setRegionError(hint);
        setCredsOpen(true);
      }
      toast.error(
        editingId
          ? `Update applied but its test failed — please verify: ${hint ?? message}`
          : `Test failed — registration rolled back: ${hint ?? message}`,
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
    setRegionError(null); // a retry re-answers the question; don't leave the last verdict up
    if (!form.model_name || !form.litellm_model) {
      toast.error('Alias and gateway model id are required');
      return;
    }
    // The server refuses an id it can't resolve a route for (it would have to guess what bills);
    // mirror that here so the failure is visible before saving, not as a 422. Gated on the ROUTABLE
    // provider: a cost-map tag inherited from the gateway is not a route the server would accept.
    if (!routableProvider) {
      toast.error('Prefix the gateway model id with its provider route (e.g. bedrock/…)');
      return;
    }
    if (aliasTaken) {
      toast.error(`'${form.model_name}' is already registered — pick a different alias`);
      return;
    }
    // Local use only — which credential params this route takes. The request carries no provider:
    // the server resolves the route itself (id prefix, else its catalog entry) and keys billing on it.
    const provider = effectiveProvider;
    // Embeddings bill input only; chat bills input/output (+ optional cache / web search).
    const units = visiblePricingUnits(form.mode, form.prices, form.supports_web_search);
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
    // Explicit capability booleans (chat only): stored model_info keys shadow the cost map, so
    // this both grants capabilities the catalog doesn't know yet (off-catalog models) and lets
    // an admin turn a catalog-claimed one off. Sent both ways — omitting a key would fall back
    // to the catalog's answer and make the toggle a no-op.
    if (form.mode === 'chat') {
      model_info.supports_reasoning = form.supports_reasoning;
      model_info.supports_web_search = form.supports_web_search;
    }

    const body: ModelRegistrationRequest = {
      model_name: form.model_name,
      litellm_params,
      ...(Object.keys(model_info).length ? { model_info } : {}),
      mode: form.mode,
      input_modes: form.input_modes,
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

      <WebSearchSettings />

      {/* Billing-provider consistency check (async — never blocks the model list) */}
      <ProviderMismatchBanner />

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
                    {m.supports_web_search && (
                      <Badge variant="outline">
                        <Globe className="mr-1 h-3 w-3" /> web search
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
                        // Chat-only capabilities — an embedding model neither thinks nor searches.
                        supports_reasoning: mode === 'embedding' ? false : f.supports_reasoning,
                        supports_web_search: mode === 'embedding' ? false : f.supports_web_search,
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
                          {/* The route it resolves to, not LiteLLM's cost-map tag: the tag
                              (`bedrock_converse`) is a vocabulary nothing here uses. */}
                          {c.family ?? c.provider} · {c.mode}
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
              {/* Bedrock availability is per-region and AWS's rejection never says which region it
                  checked, so state it here — before the admin submits and gets an "invalid model
                  identifier" they'd otherwise read as a bad id. Silent when unknowable. */}
              {bedrockRegions?.regions && (
                <p
                  className={`text-[11px] ${
                    bedrockAvailability === 'elsewhere' || bedrockAvailability === 'nowhere'
                      ? 'text-amber-700 dark:text-amber-500'
                      : 'text-muted-foreground'
                  }`}
                >
                  {bedrockAvailability === 'nowhere' ? (
                    <>
                      AWS doesn&apos;t offer this id in any checked region (
                      {(bedrockRegions.probed_regions ?? []).join(', ')}) — check the model id.
                    </>
                  ) : bedrockAvailability === 'here' ? (
                    <>
                      Available in <span className="font-mono">{effectiveBedrockRegion}</span>
                      {bedrockRegions.regions.length > 1 && (
                        <>
                          {' '}
                          (also {bedrockRegions.regions.filter((r) => r !== effectiveBedrockRegion).join(', ')})
                        </>
                      )}
                    </>
                  ) : bedrockAvailability === 'elsewhere' ? (
                    <>
                      Not offered in <span className="font-mono">{effectiveBedrockRegion}</span>
                      {form.aws_region_name.trim() ? '' : " (the gateway's region)"} — available in{' '}
                      <span className="font-mono">{bedrockRegions.regions.join(', ')}</span>. Set the AWS
                      region under “Advanced — region &amp; credentials”.
                    </>
                  ) : (
                    // Region unknown (the deployment pins none and the gateway's isn't readable):
                    // state where the model exists and stop there — claiming "not offered here" when
                    // "here" is unknown is how this line first read for a model that was available.
                    <>
                      Available in <span className="font-mono">{bedrockRegions.regions.join(', ')}</span>
                    </>
                  )}
                </p>
              )}
            </div>
            <div className="grid gap-1.5">
              <Label>Alias (what apps request)</Label>
              <Input
                placeholder="claude-sonnet-4.6"
                value={form.model_name}
                disabled={!!editingId}
                aria-invalid={aliasTaken}
                className={aliasTaken ? 'border-destructive' : undefined}
                onChange={(e) => {
                  setAliasEdited(true);
                  setForm({ ...form, model_name: e.target.value });
                }}
              />
              {!editingId && (
                <p className={`text-[11px] ${aliasTaken ? 'text-destructive' : 'text-muted-foreground'}`}>
                  {aliasTaken
                    ? `'${form.model_name}' is already registered — an alias maps to exactly one deployment. Edit or remove that model, or pick a different alias.`
                    : 'Auto-filled from the model id — edit to set a custom alias.'}
                </p>
              )}
            </div>

            <div className="grid gap-1.5">
              <Label>Provider route</Label>
              <Input
                value={effectiveProvider}
                readOnly
                disabled
                placeholder="resolved from the model id"
              />
              <p className={`text-[11px] ${routableProvider ? 'text-muted-foreground' : 'text-destructive'}`}>
                {routableProvider
                  ? derivedProvider
                    ? 'From the model id’s route prefix. This is how the gateway routes the call and how billing is keyed — change the prefix above to change it.'
                    : `This model id resolves to the ${routableProvider} route, which will be prefixed onto it on save. It’s how the gateway routes the call and how billing is keyed.`
                  : effectiveProvider
                    ? // effectiveProvider without a routable one means the value came from the gateway's
                      // cost-map TAG (litellm_provider), a vocabulary nothing routes or bills under — so
                      // it must never be promised as "will be prefixed on save": the server 422s it.
                      `“${effectiveProvider}” is this model’s cost-map tag, not a route — the gateway can’t route it and billing can’t key on it. Prefix the model id above with its provider route (e.g. bedrock/…, vertex_ai/…).`
                    : 'This model id has no route and isn’t a known catalog model — prefix it above with its provider route (e.g. bedrock/…, vertex_ai/…) so it can be routed and billed.'}
              </p>
            </div>

            {isAzureProvider(effectiveProvider) && (
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

            {(isVertexProvider(effectiveProvider) || isBedrockProvider(effectiveProvider)) && (
              <Collapsible open={credsOpen} onOpenChange={setCredsOpen}>
                <CollapsibleTrigger className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors [&[data-state=open]>svg]:rotate-180">
                  <ChevronDown className="h-4 w-4 transition-transform" />
                  Advanced — region & credentials
                </CollapsibleTrigger>
                <CollapsibleContent className="grid gap-3 pt-3">
                  {isBedrockProvider(effectiveProvider) && (
                    <div className="grid gap-1.5">
                      <Label>AWS region (optional)</Label>
                      <Input
                        placeholder={defaultBedrockRegion || 'eu-central-1'}
                        value={form.aws_region_name}
                        onChange={(e) => setForm({ ...form, aws_region_name: e.target.value })}
                      />
                      <p className="text-[11px] text-muted-foreground">
                        Leave blank to call the model in the gateway&apos;s own region
                        {defaultBedrockRegion ? (
                          <>
                            {' '}
                            (<span className="font-mono">{defaultBedrockRegion}</span>)
                          </>
                        ) : null}
                        . Bedrock model availability is per-region, so a model that isn&apos;t offered there
                        fails registration with &ldquo;The provided model identifier is invalid&rdquo; — e.g.{' '}
                        <span className="font-mono">amazon.nova-2-multimodal-embeddings-v1:0</span> is
                        us-east-1 only.
                      </p>
                    </div>
                  )}
                  {isVertexProvider(effectiveProvider) && (
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

            {form.mode === 'chat' && (
              <div className="grid gap-1.5">
                <Label>Capabilities</Label>
                <div className="flex flex-wrap gap-2">
                  <Badge
                    variant={form.supports_reasoning ? 'default' : 'outline'}
                    className="cursor-pointer"
                    onClick={() => setForm((f) => ({ ...f, supports_reasoning: !f.supports_reasoning }))}
                  >
                    <Brain className="mr-1 h-3 w-3" /> thinking
                  </Badge>
                  <Badge
                    variant={form.supports_web_search ? 'default' : 'outline'}
                    className="cursor-pointer"
                    onClick={() => setForm((f) => ({ ...f, supports_web_search: !f.supports_web_search }))}
                  >
                    <Globe className="mr-1 h-3 w-3" /> web search
                  </Badge>
                </div>
                <p className="text-[11px] text-muted-foreground">
                  Pre-filled from the gateway&apos;s catalog; set manually for models it doesn&apos;t know
                  yet. Thinking unlocks the reasoning-effort picker; web search makes the model eligible
                  to back the <span className="font-mono">console_web_search</span> tool (set its
                  per-search fee below).
                </p>
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
                {visiblePricingUnits(form.mode, form.prices, form.supports_web_search).map(({ unit, label }) => (
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

          {regionError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm">
              <div className="flex gap-2">
                <AlertTriangle className="w-4 h-4 text-destructive mt-0.5 flex-shrink-0" />
                <p className="text-muted-foreground">{regionError}</p>
              </div>
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={closeDialog}>
              Cancel
            </Button>
            <Button onClick={submit} disabled={saving || aliasTaken}>
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
