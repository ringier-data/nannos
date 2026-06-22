/**
 * Model configuration for the application.
 *
 * Live models come from the Model Gateway (GET /api/v1/models) via useAvailableModels().
 * MODEL_OPTIONS below is a static fallback used only when the gateway list can't be
 * fetched, and to resolve labels synchronously.
 */
import { useQuery } from '@tanstack/react-query';

import { getEmbeddingStatus, listAvailableModels } from '@/api/model-gateway';

export interface ModelOption {
  value: string;
  label: string;
  provider?: string;
  supportsThinking?: boolean; // Indicates if model supports extended thinking mode
  thinkingLevels?: string[]; // Per-model reasoning_effort levels (from the gateway)
}

/**
 * Available LLM models for local agents and orchestrator.
 * 
 * Model values must match the backend's expected model identifiers.
 * supportsThinking indicates which models support extended thinking mode.
 */
export const MODEL_OPTIONS: ModelOption[] = [
  { value: 'gpt-4o', label: 'GPT-4o', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'claude-sonnet-4.5', label: 'Claude Sonnet 4.5', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'claude-sonnet-4.6', label: 'Claude Sonnet 4.6', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'claude-haiku-4-5', label: 'Claude Haiku 4.5', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'gemini-3.1-pro-preview', label: 'Gemini 3.1 Pro Preview', provider: 'Google Vertex AI', supportsThinking: true },
  { value: 'gemini-3-flash-preview', label: 'Gemini 3 Flash Preview', provider: 'Google Vertex AI', supportsThinking: true },
] as const;

/**
 * Get model label by value, preferring the live gateway list (when provided) over the
 * static fallback. Returns the raw value when no label is known (e.g. a retired model).
 */
export function getModelLabel(value: string, models?: ModelOption[]): string {
  return (models ?? MODEL_OPTIONS).find(m => m.value === value)?.label || value;
}

/**
 * Get model option by value.
 */
export function getModelOption(value: string): ModelOption | undefined {
  return MODEL_OPTIONS.find(m => m.value === value);
}

/**
 * Check if a model supports extended thinking mode.
 */
export function modelSupportsThinking(modelValue: string | null | undefined, _models?: ModelOption[]): boolean {
  if (!modelValue) return false;
  const models = _models ?? MODEL_OPTIONS;
  return models.find(m => m.value === modelValue)?.supportsThinking || false;
}

/**
 * Reasoning-effort options (LiteLLM convention). "off" is modelled by the
 * enable-thinking toggle, so it's not listed here.
 */
export const THINKING_LEVEL_OPTIONS = [
  { value: 'minimal', label: 'Minimal', description: 'Fastest — least reasoning' },
  { value: 'low', label: 'Low', description: 'Balanced thinking for most tasks' },
  { value: 'medium', label: 'Medium', description: 'Deeper reasoning for complex tasks' },
  { value: 'high', label: 'High', description: 'Thorough reasoning' },
  { value: 'xhigh', label: 'Extra high', description: 'Maximum reasoning depth' },
] as const;

/**
 * Capability-tier choices for a sub-agent's model selector. The `tier:` prefix distinguishes
 * a tier selection from a concrete alias in the single model Select; on submit the form maps
 * it to the `model_tier` field (the suffix is the ModelTier value). A tier binds the agent to
 * the fleet default for that tier rather than a fixed alias.
 */
export const MODEL_TIER_OPTIONS = [
  { value: 'tier:standard', label: 'Standard tier' },
  { value: 'tier:low', label: 'Low tier — cheaper / faster' },
  { value: 'tier:premium', label: 'Premium tier — highest capability' },
] as const;

/**
 * Reasoning efforts available for a specific model, driven by what the gateway reports
 * the model supports (model.thinkingLevels). Falls back to the full set when unknown.
 */
export function getAvailableThinkingLevels(modelValue: string | null | undefined, models?: ModelOption[]) {
  const supported = (models ?? MODEL_OPTIONS).find((m) => m.value === modelValue)?.thinkingLevels;
  if (supported && supported.length > 0) {
    return THINKING_LEVEL_OPTIONS.filter((opt) => supported.includes(opt.value));
  }
  return THINKING_LEVEL_OPTIONS;
}

/**
 * Hook that returns the available models, live from the Model Gateway.
 *
 * Falls back to the static MODEL_OPTIONS when the gateway list is empty or unreachable
 * so pickers always render something. `isLoading` is exposed for callers that want it.
 */
export function useAvailableModels() {
  const { data, isLoading } = useQuery({
    queryKey: ['available-models'],
    queryFn: listAvailableModels,
    staleTime: 30_000,
  });
  const models: ModelOption[] =
    data && data.length > 0
      ? data.map((m) => ({
          value: m.value,
          label: m.label,
          provider: m.provider,
          supportsThinking: m.supports_thinking,
          thinkingLevels: m.thinking_levels ?? undefined,
        }))
      : (MODEL_OPTIONS as ModelOption[]);
  return { models, isLoading };
}

/**
 * Options for a model <Select> that keep a retired model selectable. console-backend is the
 * source of truth for `modelRetired` (a model no longer registered on the gateway); when set,
 * this prepends a "<label> (retired)" option so the trigger shows it instead of an empty field,
 * and returns `retiredValue` so callers can render a "pick a replacement" hint.
 */
export function modelSelectOptions(
  value: string | null | undefined,
  models: ModelOption[],
  modelRetired: boolean,
): { options: ModelOption[]; retiredValue: string | null } {
  if (!modelRetired || !value || models.some((m) => m.value === value)) {
    return { options: models, retiredValue: null };
  }
  return {
    options: [{ value, label: `${getModelLabel(value, models)} (retired)` }, ...models],
    retiredValue: value,
  };
}

/**
 * Whether a default embedding model is configured (text or multimodal). Embedding-dependent
 * features (catalog indexing) are disabled until an admin sets one in Admin → Model Gateway.
 *
 * Stricter than "a default row exists": the backend validates the default embedding alias is
 * also registered on the Model Gateway, so a stale default pointing at a retired/unregistered
 * model correctly reads as not-configured (otherwise a catalog looks healthy while
 * indexing/search can't actually run).
 */
export function useEmbeddingConfigured() {
  const { data, isLoading } = useQuery({
    queryKey: ['embedding-status'],
    queryFn: getEmbeddingStatus,
    staleTime: 30_000,
  });
  return { embeddingConfigured: !!data?.ready, embeddingStatus: data, isLoading };
}
