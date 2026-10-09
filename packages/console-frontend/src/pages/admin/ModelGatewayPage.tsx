import { useEffect, useMemo, useRef, useState } from 'react';
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
import { useObjectStateAdapter, type ActionOutcome } from '@nannos/embed-sdk';

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
  type CostPrefill,
  type DefaultRole,
  type GatewayModel,
  type ModelRegistrationRequest,
  type ProbeEvent,
  type RateCardPricingEntry,
  roleLabel,
  probeInconclusive,
  probeLimitations,
  recordedLimitations,
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
import { ProbeProgress } from '@/components/admin/ProbeProgress';
import { initialProbeState, reduceProbe, type ProbeState } from '@/lib/probeProgress';
import { ProviderMismatchBanner } from '@/components/admin/ProviderMismatchBanner';
import { PROVIDER_CONFIG_QUERY_KEY } from '@/lib/providerCheckQuery';
import {
  compatibleBaseModels,
  hasWebSearchFee,
  pricePerMillion,
  pricesFromCatalogEntry,
  routeOf,
} from '@/lib/catalogPricing';
import { FailoverChains } from '@/components/admin/FailoverChains';
import { WebSearchSettings } from '@/components/admin/WebSearchSettings';
import { NannosForm } from '@/components/nannos/NannosForm';
import { NannosActions } from '@/components/nannos/NannosActions';
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
/** The shapes the probe could not measure: any earlier result for them is kept, so re-run Test. */
function inconclusiveMessage(name: string, shapes: { shape: string; error: string }[]): string {
  return (
    `${name}: the probe could not measure ` +
    shapes.map((s) => `${s.shape.replace(/_/g, ' ')} (${s.error || 'transient failure'})`).join(', ') +
    ` — any earlier result for them is kept; re-run Test.`
  );
}

/** One line per shape the probe saw the model reject, with the provider's reason. */
/** One line of what a finished run has to say, shown in the run window. */
interface ProbeNote {
  tone: 'success' | 'warning' | 'error';
  text: string;
}

/** A save-and-test or a card's Test, shown in the run window from submit to Close. */
interface ProbeRun {
  kind: 'register' | 'edit' | 'test';
  model: string;
  /** saving: the rate card + deployment write; probing: the Test stream; done: verdict in. */
  phase: 'saving' | 'probing' | 'done';
  state: ProbeState;
  notes: ProbeNote[];
  /** The form's values are kept for another try (a failed save). */
  backToForm: boolean;
}

/** The warnings a passing probe leaves — each a note, where they used to be one toast each. */
function probeNotes(
  name: string,
  r: {
    limitations: ReturnType<typeof probeLimitations>;
    inconclusive: ReturnType<typeof probeInconclusive>;
    recorded: boolean | null;
    warning: string | null;
  },
): ProbeNote[] {
  const notes: ProbeNote[] = [];
  if (r.limitations.length) notes.push({ tone: 'warning', text: limitationsMessage(name, r.limitations) });
  if (r.inconclusive.length) notes.push({ tone: 'warning', text: inconclusiveMessage(name, r.inconclusive) });
  if (r.recorded === false)
    notes.push({ tone: 'warning', text: `The probe's result could not be recorded on the gateway — re-run Test.` });
  if (r.warning) notes.push({ tone: 'error', text: r.warning });
  return notes;
}

/** A re-registering edit whose replaced deployment survived (`updated_with_stale_duplicate`). */
function staleDuplicateNote(name: string, staleId: string): ProbeNote {
  return {
    tone: 'error',
    text:
      `The deployment this edit replaced (${staleId}) could not be removed and still serves ${name} next to ` +
      `the new one, so part of its traffic goes to the old settings. Remove it on this page, then re-run Test.`,
  };
}

function limitationsMessage(name: string, limitations: { shape: string; label?: string; error: string }[]): string {
  // The full provider reasons are in the probe's progress view; a toast gets the gist.
  const clip = (s: string) => (s.length > 160 ? `${s.slice(0, 157)}…` : s);
  return (
    `${name} has limitations the gateway routes around: ` +
    limitations
      .map((l) =>
        // ringier_a2a_sdk.model_capabilities words an always-on verdict this way.
        l.shape === 'thinking_off' && l.error.startsWith('thinking cannot be turned off')
          ? 'it always thinks — nothing turns thinking off, so pickers offer a level but no "off"'
          : `${l.label ?? l.shape.replace(/_/g, ' ')} (${clip(l.error || 'rejected')})`,
      )
      .join('; ')
  );
}

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
  // when the web-search capability is on or the form already holds a price for it, so it isn't a
  // confusing empty field on chat models that can't search. See webSearchOnly gating below.
  { unit: 'web_search', label: 'Web search ($/M searches)', flow: 'output', webSearchOnly: true },
];

// The pricing fields shown/submitted for a given mode. web_search is shown when the capability
// toggle is on or the form already holds a fee for it; everything else follows the
// input/embedding split.
const visiblePricingUnits = (mode: string, prices: Record<string, string>, canSearch = false) =>
  (mode === 'embedding'
    ? PRICING_UNITS.filter((u) => u.flow === 'input')
    : PRICING_UNITS.filter((u) => !u.embeddingOnly)
  ).filter((u) => !u.webSearchOnly || canSearch || prices[u.unit] != null);

// A cost-prefill response as form prices (billing unit → per-million string, trailing zeros dropped).
const pricesFromPrefill = (pricing: CostPrefill['pricing']): Record<string, string> =>
  Object.fromEntries(Object.entries(pricing ?? {}).map(([unit, e]) => [unit, String(Number(e.price_per_million))]));

type FormState = {
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
};

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
// One definition (`routeOf`), shared with the base-model compatibility filter so the two can
// never parse a route differently.
const deriveProvider = routeOf;

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

export function ModelGatewayPage() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  // The running (or last) save-and-test or Test, shown in its own window: the form closes when
  // the admin submits, and everything the run has to say — each shape's verdict, the failure's
  // reason, the warnings that used to be toasts — stays readable there until they close it.
  const [run, setRun] = useState<ProbeRun | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  const [confirmCloseRun, setConfirmCloseRun] = useState(false);
  // Read from mutation callbacks, which close over a render that may be stale by the time the
  // probe ends: a run that finishes after its window was closed says so in a toast instead.
  const runOpenRef = useRef(false);
  useEffect(() => {
    runOpenRef.current = runOpen;
  }, [runOpen]);
  const startRun = (kind: ProbeRun['kind'], model: string) => {
    setRun({ kind, model, phase: kind === 'test' ? 'probing' : 'saving', state: initialProbeState(model), notes: [], backToForm: false });
    setRunOpen(true);
  };
  const probeStarted = (model: string) =>
    setRun((r) => (r && r.model === model ? { ...r, phase: 'probing' } : r));
  const onProbeEvent = (model: string) => (event: ProbeEvent) =>
    setRun((r) => (r && r.model === model ? { ...r, state: reduceProbe(r.state, event) } : r));
  const finishRun = (model: string, notes: ProbeNote[], backToForm = false) => {
    setRun((r) => (r && r.model === model ? { ...r, phase: 'done', notes, backToForm } : r));
    if (!runOpenRef.current) {
      const worst = notes.find((n) => n.tone === 'error') ?? notes.find((n) => n.tone === 'warning') ?? notes[0];
      const show = worst?.tone === 'error' ? toast.error : worst?.tone === 'warning' ? toast.warning : toast.success;
      show(worst?.text ?? `${model}: finished`, {
        duration: 12000,
        action: { label: 'Details', onClick: () => setRunOpen(true) },
      });
    }
  };
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  // Units the last "Pre-fill from gateway" changed: the value before and the gateway's value. A
  // field shows its "was" hint only while it still holds the gateway's value, so editing it by hand
  // drops the hint on its own; re-seeding from a catalog or base model clears it explicitly.
  const [prefillDiff, setPrefillDiff] = useState<Record<string, { was: string; now: string }>>({});
  // The edit dialog's stored-rate load is in flight. Pre-fill waits for it: diffing against the
  // still-empty form would mark every unit "was empty", and the load would then be dropped.
  const [ratesLoading, setRatesLoading] = useState(false);
  // Which dialog opening the in-flight stored-rate load belongs to: a load that resolves after its
  // dialog was closed (or another model opened) must neither seed nor re-enable the button.
  const ratesLoadSeq = useRef(0);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [basePickerOpen, setBasePickerOpen] = useState(false);
  // The base-model list filters only on text typed since the field was focused: a value already
  // in place (picked, or loaded on edit) must not hide the other tiers of the same model.
  const [baseTyped, setBaseTyped] = useState(false);
  // The base model as it was on focus. Leaving the field with a DIFFERENT compatible catalog id
  // (pasted, or edited to another tier) re-seeds the prices; typing through an id or retyping the
  // same one does not, so stored rates in edit mode are only replaced by a deliberate change.
  const baseOnFocus = useRef('');
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
  // The replaced deployment a re-registering edit could not delete, for the run's verdict.
  const staleDuplicateRef = useRef<string | null>(null);
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
  // A stable order for the grid: the gateway lists a deployment LAST after any write to it (a
  // Test records its probe with PATCH /model/{id}/update), so its own order moved a card out
  // from under the admin's cursor. Config models first, then by alias; the id breaks ties.
  const sortedModels = useMemo(
    () =>
      [...models].sort(
        (a, b) =>
          Number(!!a.db_model) - Number(!!b.db_model) ||
          a.model_name.localeCompare(b.model_name) ||
          (a.model_id ?? '').localeCompare(b.model_id ?? ''),
      ),
    [models],
  );

  // LiteLLM's known-model catalog: every provider, each entry annotated with the route it bills under.
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

  // Routes this gateway already serves. The catalog lists every LiteLLM provider, and whether this
  // deployment holds credentials for one is only known to the proxy — so matches from a route already
  // in use rank first, and typing "claude-sonnet" doesn't open on resellers.
  const servedRoutes = useMemo(() => new Set(models.map((m) => m.provider).filter(Boolean)), [models]);
  const isServed = (c: CatalogModel) => servedRoutes.has(c.family ?? '') || servedRoutes.has(c.provider ?? '');

  // Picker matches: scoped to the chosen mode, substring-filtered on what's typed, served routes
  // first (a stable sort, so the catalog's order holds within each group), capped.
  const q = form.litellm_model.trim().toLowerCase();
  const catalogMatches = catalog
    .filter((c) => c.mode === form.mode && (q === '' || c.model_id.toLowerCase().includes(q)))
    .sort((a, b) => Number(isServed(b)) - Number(isServed(a)));
  const visibleMatches = catalogMatches.slice(0, CATALOG_LIMIT);

  // Base-model picker: only entries compatible with the gateway id (same route; the same model's
  // region/date variants when the id names a known model), substring-filtered on what's typed.
  const compatibleBases = compatibleBaseModels(catalog, form.litellm_model.trim(), form.mode);
  const bq = baseTyped ? form.base_model.trim().toLowerCase() : '';
  const baseMatches = compatibleBases.filter((c) => bq === '' || c.model_id.toLowerCase().includes(bq));
  const visibleBaseMatches = baseMatches.slice(0, CATALOG_LIMIT);

  // Selecting a catalog model pre-fills the gateway id, provider, input modes and cost.
  const applyCatalogEntry = (entry: CatalogModel) => {
    setPrefillDiff({}); // the prices below replace the form's; a gateway "was" no longer applies
    const modes = ['text'];
    if (entry.supports_vision) modes.push('image');
    if (entry.supports_audio_input) modes.push('audio');
    if (entry.supports_pdf_input) modes.push('file');
    const isEmbedding = entry.mode === 'embedding';
    setForm((f) => {
      // A base model already chosen and still compatible with the new id keeps pricing the
      // deployment: it names the tier (e.g. EU Data Zone), which the id alone cannot.
      const base = catalog.find((c) => c.model_id === f.base_model.trim());
      const pricedBy =
        base && compatibleBaseModels(catalog, entry.model_id, entry.mode ?? 'chat').includes(base) ? base : entry;
      return {
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
        // Same entry as the prices, so a web-search fee and the capability can't disagree.
        supports_web_search: !isEmbedding && (!!pricedBy.supports_web_search || hasWebSearchFee(pricedBy)),
        // A catalog base model that no longer fits the new id is dropped rather than submitted:
        // the gateway would read max-tokens and cost metadata off a different model. Off-catalog
        // free text is kept; nothing here can tell whether it fits.
        base_model: base && pricedBy !== base ? '' : f.base_model,
        // Replace (not merge): selecting a different model must not leave a prior model's prices —
        // e.g. a stale web_search fee on a model that can't search, or stale cache rates.
        prices: pricesFromCatalogEntry(pricedBy),
      };
    });
  };

  // A base model names the priced catalog entry for a deployment whose id can't (an Azure
  // deployment name says which model, not which tier). Choosing one re-seeds the prices from it,
  // replacing the gateway id's: the base model is the more specific answer.
  const applyBaseModelEntry = (entry: CatalogModel) => {
    setPrefillDiff({});
    setForm((f) => ({ ...f, base_model: entry.model_id, prices: pricesFromCatalogEntry(entry) }));
  };

  // Assistant writes go through the same paths as typing. An alias it sets is pinned like a hand-edited
  // one; within one apply pass `aliasEdited` hasn't re-rendered yet, so the ref re-asserts it over a
  // catalog pick applied after it.
  const agentAlias = useRef<string | null>(null);
  const nannosForm = useObjectStateAdapter(form, (next) => {
    const { model_name, litellm_model, ...rest } = next;
    if (model_name !== undefined && !editingId) {
      setAliasEdited(true);
      agentAlias.current = model_name;
      queueMicrotask(() => {
        agentAlias.current = null;
      });
      setForm((f) => ({ ...f, model_name }));
    }
    if (litellm_model !== undefined) {
      const entry = catalog.find((c) => c.model_id === litellm_model);
      if (entry) {
        applyCatalogEntry(entry);
        const pinned = agentAlias.current;
        if (pinned !== null) setForm((f) => ({ ...f, model_name: pinned }));
      } else {
        setForm((f) => ({ ...f, litellm_model, provider: deriveProvider(litellm_model) || f.provider }));
      }
    }
    if (Object.keys(rest).length) setForm((f) => ({ ...f, ...rest }));
  });

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
    ratesLoadSeq.current += 1;
    setRatesLoading(false);
    setDialogOpen(false);
    setEditingId(null);
    setForm(EMPTY_FORM);
    setPrefillDiff({});
    setRegionError(null);
  };

  const openCreate = () => {
    ratesLoadSeq.current += 1;
    setRatesLoading(false);
    setEditingId(null);
    setForm(EMPTY_FORM);
    setPrefillDiff({});
    setCredsOpen(false);
    setAliasEdited(false);
    setDialogOpen(true);
  };

  const openEdit = async (m: GatewayModel) => {
    setEditingId(m.model_id ?? null);
    setPrefillDiff({});
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
    const seq = ++ratesLoadSeq.current;
    setRatesLoading(true);
    try {
      const prices = pricesFromPrefill((await getCostPrefill(m.model_name)).pricing);
      if (seq !== ratesLoadSeq.current) return;
      // The dialog is already open while this loads: if the admin picked a base model (or typed
      // a price) in the meantime, those prices are newer than the stored ones and win.
      setForm((f) => (Object.keys(f.prices).length ? f : { ...f, prices }));
    } catch {
      /* no seed — admin enters rates */
    } finally {
      if (seq === ratesLoadSeq.current) setRatesLoading(false);
    }
  };

  // Save = persist, then validate with a live ping before we consider the model usable.
  // A newly-registered model that fails the test is deleted again, so a failed save never
  // leaves a broken alias behind. Edits apply first and aren't rolled back (we hold no
  // snapshot of the prior params) — the admin is told the change landed but failed its test.
  const saveMutation = useMutation({
    mutationFn: async (body: ModelRegistrationRequest) => {
      staleDuplicateRef.current = null;
      // The form steps aside (its values kept, for "Back to form") and the run window takes over.
      setDialogOpen(false);
      startRun(editingId ? 'edit' : 'register', body.model_name);
      if (editingId) {
        const startedFrom = editingId;
        const updated = await updateGatewayModel(startedFrom, body);
        // An edit keeps the deployment id unless it re-registered (it made the deployment another
        // model, or cleared a field); then
        // the form moves onto the new id so "Back to form" edits the live deployment — but only
        // while it is still this model's form (the run window can be closed mid-save).
        const newId = updated.gateway_model_id;
        if (newId && newId !== startedFrom) setEditingId((cur) => (cur === startedFrom ? newId : cur));
        if (updated.status === 'updated_with_stale_duplicate') staleDuplicateRef.current = updated.stale_duplicate_model_id ?? null;
        probeStarted(body.model_name);
        // Throws when the probe refuses; the record goes to the re-registered deployment.
        const test = await testGatewayModel(body.model_name, updated.gateway_model_id, onProbeEvent(body.model_name));
        return {
          name: body.model_name,
          created: null as GatewayModel | null,
          limitations: probeLimitations(test.probe),
          inconclusive: probeInconclusive(test.probe),
          recorded: test.recorded ?? null,
          warning: test.warning ?? null,
        };
      }
      const res = await registerGatewayModel(body);
      let limitations: ReturnType<typeof probeLimitations> = [];
      let inconclusive: ReturnType<typeof probeInconclusive> = [];
      let recorded: boolean | null = null;
      let warning: string | null = null;
      let capabilities: Record<string, unknown> | null = null;
      try {
        // Throws when the model rejects a shape every agent turn sends (or could not be
        // measured); a shape the harness can route around is recorded on the deployment and
        // reported here instead.
        probeStarted(body.model_name);
        const test = await testGatewayModel(res.model_name, res.gateway_model_id, onProbeEvent(body.model_name));
        limitations = probeLimitations(test.probe);
        inconclusive = probeInconclusive(test.probe);
        recorded = test.recorded ?? null;
        warning = test.warning ?? null;
        capabilities = test.probe?.capabilities ?? null;
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
        // The record the probe just wrote — only when it was actually written; otherwise every
        // reader treats the model as unprobed. (A re-probe merges over a stored record, but a
        // fresh registration has none, so the probe's own flags are the record.)
        capabilities: recorded === true ? capabilities : null,
      };
      // First model to serve a role becomes the fleet default automatically, so a fresh
      // system always has a fallback without a separate "Make default" click. Only fill
      // roles that nothing already holds (config or db model) — never steal an existing
      // default. Capability tiers are EXCLUDED from auto-assignment: which model is the
      // low/premium tier is an explicit admin decision, not something a new model silently
      // grabs. Best-effort: a failed set must not roll back the good registration, and it
      // runs after the test so we never default an alias we're about to delete.
      // A model the probe saw reject response_format can't hold the chat default either: the
      // server refuses it (every classifier and summarizer call would break), so don't ask.
      const utilityCapable = !limitations.some((l) => l.shape === 'response_format');
      const autoRoles = defaultRolesFor(created).filter(
        (role) => role !== 'chat:low' && role !== 'chat:premium' && (role !== 'chat' || utilityCapable),
      ).filter(
        (role) => !models.some((m) => (m.default_roles ?? []).includes(role)),
      );
      if (res.gateway_model_id && autoRoles.length) {
        for (const role of autoRoles) {
          await setGatewayModelDefault(res.gateway_model_id, role).catch(() => {});
        }
        created.default_roles = autoRoles;
      }
      return { name: res.model_name, created, limitations, inconclusive, recorded, warning };
    },
    onSuccess: ({ name, created, limitations, inconclusive, recorded, warning }, body) => {
      const auto = created?.default_roles ?? [];
      const stale = staleDuplicateRef.current;
      finishRun(body.model_name, [
        stale
          ? staleDuplicateNote(name, stale)
          : {
              tone: 'success',
              text: auto.length
                ? `Saved & tested ${name} — set as default ${auto.map((r) => r.replace('_', ' ')).join(' & ')}.`
                : `Saved & tested ${name}.`,
            },
        ...probeNotes(name, { limitations, inconclusive, recorded, warning }),
      ]);
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
    onError: (e: unknown, body) => {
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
      finishRun(
        body.model_name,
        [
          {
            tone: 'error',
            text: editingId
              ? `The update was applied but its test failed — please verify: ${hint ?? message}`
              : `Test failed — the registration was rolled back: ${hint ?? message}`,
          },
          ...(staleDuplicateRef.current ? [staleDuplicateNote(body.model_name, staleDuplicateRef.current)] : []),
        ],
        true,
      );
      invalidate(); // an edit may have landed; reflect the gateway's real state
    },
  });

  const testMutation = useMutation({
    mutationFn: ({ name, modelId }: { name: string; modelId?: string | null }) => {
      startRun('test', name);
      return testGatewayModel(name, modelId, onProbeEvent(name));
    },
    onSuccess: (r, { name }) => {
      const limitations = probeLimitations(r.probe);
      const inconclusive = probeInconclusive(r.probe);
      const notes = probeNotes(name, {
        limitations,
        inconclusive,
        recorded: r.probe ? (r.recorded ?? null) : null,
        warning: r.warning ?? null,
      });
      finishRun(
        name,
        notes.length
          ? notes
          : [
              {
                tone: 'success',
                text: r.probe ? `${name} accepts every request shape the harness sends.` : `Test call to ${name} succeeded.`,
              },
            ],
      );
      invalidate(); // the probe re-recorded the model's capabilities
    },
    onError: (e: unknown, { name }) => finishRun(name, [{ tone: 'error', text: `Test failed: ${errMsg(e)}` }]),
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
    onSuccess: (result, { role }) => {
      toast.success(`Set as default ${role.replace('_', ' ')} (apps pick it up within ~60s)`);
      // The default is stored even when its failover chain could not be re-declared on the
      // proxy — a partial success the admin has to see, or the tier silently keeps routing to
      // the previous head's chain until someone happens to open the failover card.
      if (result?.warning) toast.warning(result.warning);
      invalidate();
    },
    onError: (e: unknown) => toast.error(`Set default failed: ${errMsg(e)}`),
  });

  // Always the gateway's cost, never the stored rate card: the dialog already loaded that on open,
  // so this is how a card missing units (e.g. cache rates) or holding a stale price gets corrected.
  // Only units the gateway knows are overwritten; nothing is saved until "Save changes".
  const prefill = async () => {
    if (!form.model_name) return;
    const seq = ratesLoadSeq.current; // a dialog closed or reopened meanwhile must not receive these
    let prices: Record<string, string>;
    try {
      prices = pricesFromPrefill((await getCostPrefill(form.model_name, 'gateway')).pricing);
    } catch (e) {
      if (seq === ratesLoadSeq.current) toast.error(`Pre-fill failed: ${errMsg(e)}`);
      return;
    }
    if (seq !== ratesLoadSeq.current) return;
    // Only the units this form shows and saves: an embedding model has no output price, and a
    // web-search fee is only added once the capability is on (one already shown is corrected).
    const shown = new Set(visiblePricingUnits(form.mode, form.prices, form.supports_web_search).map((u) => u.unit));
    prices = Object.fromEntries(Object.entries(prices).filter(([unit]) => shown.has(unit)));
    if (Object.keys(prices).length === 0) {
      toast.info('Gateway has no cost for this model yet — enter rates manually');
      return;
    }
    const diff: Record<string, { was: string; now: string }> = {};
    for (const [unit, now] of Object.entries(prices)) {
      const was = form.prices[unit] ?? '';
      if (was === '' || Number(was) !== Number(now)) diff[unit] = { was, now };
    }
    setForm((f) => ({ ...f, prices: { ...f.prices, ...prices } }));
    setPrefillDiff(diff);
    const label = (unit: string) =>
      (PRICING_UNITS.find((u) => u.unit === unit)?.label ?? unit).replace(/ \(.*\)$/, '').toLowerCase();
    const filled = Object.keys(diff)
      .filter((u) => diff[u].was === '')
      .map(label);
    const updated = Object.keys(diff)
      .filter((u) => diff[u].was !== '')
      .map(label);
    if (Object.keys(diff).length === 0) toast.success('Rates already match the gateway');
    else
      toast.success(
        [filled.length && `Filled ${filled.join(', ')}`, updated.length && `Updated ${updated.join(', ')}`]
          .filter(Boolean)
          .join(' · ') + ' from the gateway — review, then save'
      );
  };

  // The run window reports the outcome to the user; the resolved value reports it to the assistant.
  const submit = async (): Promise<ActionOutcome> => {
    setRegionError(null); // a retry re-answers the question; don't leave the last verdict up
    if (!form.model_name || !form.litellm_model) {
      toast.error('Alias and gateway model id are required');
      return { ok: false, detail: 'Alias and gateway model id are required' };
    }
    // The server refuses an id it can't resolve a route for (it would have to guess what bills);
    // mirror that here so the failure is visible before saving, not as a 422. Gated on the ROUTABLE
    // provider: a cost-map tag inherited from the gateway is not a route the server would accept.
    if (!routableProvider) {
      toast.error('Prefix the gateway model id with its provider route (e.g. bedrock/…)');
      return { ok: false, detail: 'Prefix the gateway model id with its provider route (e.g. bedrock/…)' };
    }
    if (aliasTaken) {
      toast.error(`'${form.model_name}' is already registered — pick a different alias`);
      return { ok: false, detail: `'${form.model_name}' is already registered — pick a different alias` };
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
      return { ok: false, detail: 'Set at least one price — a model must be billable before it can be used' };
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
    const wasEditing = Boolean(editingId);
    try {
      await saveMutation.mutateAsync(body);
      return true;
    } catch (e) {
      return {
        ok: false,
        detail: wasEditing
          ? `The update or its test failed (the update may already be applied, verify it): ${errMsg(e)}`
          : `Test failed, the registration was rolled back: ${errMsg(e)}`,
      };
    }
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
      {/* The create button, for the assistant: without it the agent could only ask the user to
          click it. It opens the GatewayModel form, which takes this type:id while the dialog is open. */}
      {!dialogOpen && (
        <NannosActions
          type="GatewayModel"
          id={undefined}
          actions={{
            create: {
              label: 'Register model',
              description: 'Open the Register model dialog with an empty, unsaved form; then fill it and submit.',
              run: openCreate,
            },
            // The "Make default" / "Default low tier" buttons save at once: offered with
            // requiresApproval, so the agent's call waits for the admin's click on a card.
            // Embedding defaults are left out: switching one needs the re-index warning the
            // admin confirms in its own dialog.
            set_default: {
              label: 'Set as default',
              description:
                "Make a listed chat model the default for a role, like its 'Make default' / 'Default low tier' / " +
                "'Default premium tier' button. Saves immediately (the user approves).",
              requiresApproval: true,
              params: [
                { name: 'model_name', type: 'string', description: 'The model alias as listed' },
                {
                  name: 'role',
                  type: 'string',
                  enum: ['chat', 'chat:low', 'chat:premium'],
                  description: 'chat = the standard default; chat:low / chat:premium = the low / premium tier',
                },
              ],
              run: async ({ model_name, role }) => {
                const m = models.find((x) => x.model_name === model_name);
                if (!m?.model_id) return { ok: false, detail: `No listed model is called ${String(model_name)}.` };
                const wanted = role as DefaultRole;
                if (isEmbeddingRole(wanted) || !defaultRolesFor(m).includes(wanted)) {
                  return { ok: false, detail: `${m.model_name} cannot be the default for ${String(role)} here.` };
                }
                try {
                  const result = await defaultMutation.mutateAsync({ modelId: m.model_id, role: wanted });
                  return result?.warning ? { ok: true, detail: result.warning } : true;
                } catch (e) {
                  return { ok: false, detail: errMsg(e) };
                }
              },
            },
          }}
        />
      )}
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

      <FailoverChains />

      {/* Billing-provider consistency check (async — never blocks the model list) */}
      <ProviderMismatchBanner />

      {isLoading ? (
        <p className="text-muted-foreground">Loading…</p>
      ) : models.length === 0 ? (
        <p className="text-muted-foreground">No models registered yet.</p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {sortedModels.map((m: GatewayModel) => {
            const testing = testMutation.isPending && testMutation.variables?.name === m.model_name;
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
                  {/* The deployment id: what the run window names when an edit leaves a second
                      deployment under an alias, and the only way to tell such cards apart. */}
                  {m.db_model && m.model_id && (
                    <CardDescription className="font-mono text-[11px] break-all" title="Gateway deployment id">
                      id {m.model_id}
                    </CardDescription>
                  )}
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
                    {recordedLimitations(m).length > 0 && (
                      <Badge
                        variant="outline"
                        title={`Registration probe: ${recordedLimitations(m).join(', ')}. The gateway routes around these; the model cannot hold the chat or chat:low default if it rejects response_format.`}
                      >
                        <AlertTriangle className="mr-1 h-3 w-3" /> {recordedLimitations(m).join(' · ')}
                      </Badge>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2 border-t pt-3">
                    <Button
                      size="sm"
                      variant="outline"
                      // The row's own id, always: the backend judges writability from the deployment it
                      // looks up (a config-defined one is probed and reported, `recorded: null`), and an
                      // alias lookup could land on a sibling deployment.
                      onClick={() => testMutation.mutate({ name: m.model_name, modelId: m.model_id })}
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
        <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
          <NannosForm type="GatewayModel" id={editingId ?? undefined} form={nannosForm} save={submit} />
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
                <Label>
                  Base model (Azure){compatibleBases.length > 0 ? ' — compatible catalog models, type to filter' : ''}
                </Label>
                <div className="relative">
                  <Input
                    placeholder="azure/eu/gpt-6-sol"
                    value={form.base_model}
                    autoComplete="off"
                    onFocus={() => {
                      baseOnFocus.current = form.base_model.trim();
                      setBaseTyped(false);
                      setBasePickerOpen(true);
                    }}
                    onBlur={(e) => {
                      setTimeout(() => setBasePickerOpen(false), 150);
                      // A pasted or edited value that names a different compatible tier re-prices
                      // the deployment; looked up in the unfiltered list, not the typed-filtered one.
                      const v = e.target.value.trim();
                      const entry = compatibleBases.find((c) => c.model_id === v);
                      if (entry && v !== baseOnFocus.current) applyBaseModelEntry(entry);
                    }}
                    onChange={(e) => {
                      setBasePickerOpen(true);
                      setBaseTyped(true);
                      setForm({ ...form, base_model: e.target.value });
                    }}
                  />
                  {basePickerOpen && visibleBaseMatches.length > 0 && (
                    <div className="absolute z-50 mt-1 max-h-64 w-full overflow-auto rounded-md border bg-popover p-1 shadow-md">
                      {visibleBaseMatches.map((c) => (
                        <button
                          type="button"
                          key={c.model_id}
                          className="flex w-full flex-col items-start rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent hover:text-accent-foreground"
                          onMouseDown={(e) => {
                            e.preventDefault(); // keep focus / beat onBlur so the click registers
                            applyBaseModelEntry(c);
                            // The pick is the change: blur must not re-apply it, and the list
                            // reopens unfiltered.
                            baseOnFocus.current = c.model_id;
                            setBaseTyped(false);
                            setBasePickerOpen(false);
                          }}
                        >
                          <span className="font-mono text-xs">{c.model_id}</span>
                          <span className="text-muted-foreground text-[11px]">
                            {/* The precision the price fields get, so a regional uplift stays visible. */}
                            {pricePerMillion(c.input_cost_per_token)
                              ? `$${pricePerMillion(c.input_cost_per_token)}/M`
                              : 'no input price'}{' '}
                            in ·{' '}
                            {pricePerMillion(c.output_cost_per_token)
                              ? `$${pricePerMillion(c.output_cost_per_token)}/M`
                              : 'no output price'}{' '}
                            out
                          </span>
                        </button>
                      ))}
                      {baseMatches.length > visibleBaseMatches.length && (
                        <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
                          +{baseMatches.length - visibleBaseMatches.length} more — keep typing to narrow
                        </div>
                      )}
                    </div>
                  )}
                </div>
                <p className="text-[11px] text-muted-foreground">
                  The catalog model this deployment serves, including its pricing tier (e.g.{' '}
                  <span className="font-mono">azure/eu/gpt-6-sol</span> for an EU Data Zone deployment).
                  Choosing one pre-fills the prices below from it, and lets the gateway identify the
                  deployment for max-tokens and native cost tracking.
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
                <Button type="button" size="sm" variant="ghost" onClick={prefill} disabled={ratesLoading}>
                  Pre-fill from gateway
                </Button>
              </div>
              <div className="grid grid-cols-2 gap-2">
                {visiblePricingUnits(form.mode, form.prices, form.supports_web_search).map(({ unit, label }) => {
                  const changed = prefillDiff[unit]?.now === form.prices[unit] ? prefillDiff[unit] : undefined;
                  return (
                    <div key={unit} className="grid gap-1">
                      <Label className="text-xs text-muted-foreground">{label}</Label>
                      <Input
                        type="number"
                        step="0.0001"
                        className={changed ? 'border-amber-500 focus-visible:ring-amber-500' : undefined}
                        value={form.prices[unit] ?? ''}
                        onChange={(e) => setForm({ ...form, prices: { ...form.prices, [unit]: e.target.value } })}
                      />
                      {changed && (
                        <p className="text-xs text-amber-700 dark:text-amber-400">
                          {changed.was === '' ? 'was empty' : `was ${Number(changed.was)}`}
                        </p>
                      )}
                    </div>
                  );
                })}
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

      {/* The run window: a save-and-test or a card's Test, from submit until the admin closes it.
          Everything the run has to say is here, so nothing flashes past in a toast. */}
      <Dialog
        open={runOpen && !!run}
        onOpenChange={(o) => (o ? setRunOpen(true) : run?.phase === 'done' ? setRunOpen(false) : setConfirmCloseRun(true))}
      >
        <DialogContent className="max-w-lg max-h-[90vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>
              {run?.kind === 'register' ? 'Registering' : run?.kind === 'edit' ? 'Saving' : 'Testing'} {run?.model}
            </DialogTitle>
            <DialogDescription>
              {run?.kind === 'test'
                ? 'Replays the request shapes every agent turn sends and records what this model accepts.'
                : 'Writes the rate card and the gateway deployment, then replays the request shapes every agent turn sends and records what the model accepts.'}
            </DialogDescription>
          </DialogHeader>
          {run && (
            // overflow-x-hidden: a spinning icon's rotated corners count as overflow, and with
            // overflow-y auto the browser makes x auto too — a scrollbar flashed on every turn.
            // The stable gutter keeps the width from changing when the list reaches max height.
            <div className="min-h-0 space-y-4 overflow-y-auto overflow-x-hidden pb-1 [scrollbar-gutter:stable]">
              {run.phase === 'saving' && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <span className="flex h-4 w-4 flex-shrink-0 overflow-hidden">
                    <Loader2 className="h-4 w-4 animate-spin" />
                  </span>
                  {run.kind === 'register' ? 'Writing the rate card and the gateway deployment…' : 'Saving the deployment…'}
                </div>
              )}
              {run.phase !== 'saving' && (run.state.rows.length > 0 || run.phase === 'done') && (
                <ProbeProgress state={run.state} />
              )}
              {run.phase === 'probing' && run.state.rows.length === 0 && (
                // Until the plan lands (a few ms for a chat model), or for an embedding ping.
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <span className="flex h-4 w-4 flex-shrink-0 overflow-hidden">
                    <Loader2 className="h-4 w-4 animate-spin" />
                  </span>
                  Testing {run.model}…
                </div>
              )}
              {run.notes.map((n, i) => (
                <div
                  key={i}
                  className={
                    n.tone === 'error'
                      ? 'rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm break-words'
                      : n.tone === 'warning'
                        ? 'rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-sm break-words'
                        : 'rounded-md border border-green-600/30 bg-green-600/5 px-3 py-2 text-sm break-words'
                  }
                >
                  {n.text}
                </div>
              ))}
            </div>
          )}
          <DialogFooter>
            {run?.phase === 'done' && run.backToForm && (
              <Button
                variant="outline"
                onClick={() => {
                  setRunOpen(false);
                  setDialogOpen(true);
                }}
              >
                Back to form
              </Button>
            )}
            <Button
              variant={run?.phase === 'done' ? 'default' : 'outline'}
              onClick={() => (run?.phase === 'done' ? setRunOpen(false) : setConfirmCloseRun(true))}
            >
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmCloseRun}
        onOpenChange={setConfirmCloseRun}
        title="Close while it runs?"
        description={
          run?.kind === 'test'
            ? 'The probe keeps running on the server and records its result. You will get a notification when it finishes.'
            : 'The save keeps running as long as this page stays open, and the probe records its result. You will get a notification when it finishes.'
        }
        confirmLabel="Close"
        cancelLabel="Keep watching"
        onConfirm={() => {
          setConfirmCloseRun(false);
          setRunOpen(false);
        }}
      />

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
