/**
 * Model configuration for the application.
 *
 * Models are fetched dynamically from the backend via GET /api/v1/models.
 * The FALLBACK_MODEL_OPTIONS are used only while the API response is loading
 * or if the fetch fails.
 */

import { useQuery } from '@tanstack/react-query';
import { listAvailableModelsApiV1ModelsGetOptions } from '@/api/generated/@tanstack/react-query.gen';
import type { AvailableModel } from '@/api/generated/types.gen';

export interface ModelOption {
  value: string;
  label: string;
  provider?: string;
  supportsThinking?: boolean; // Indicates if model supports extended thinking mode
  thinkingLevels?: string[]; // Available thinking levels (if restricted)
  isDefault?: boolean; // Whether this is the orchestrator's default model
}

/** Map an SDK AvailableModel (snake_case) to a ModelOption (camelCase). */
function toModelOption(m: AvailableModel): ModelOption {
  return {
    value: m.value,
    label: m.label,
    provider: m.provider,
    supportsThinking: m.supports_thinking,
    thinkingLevels: m.thinking_levels ?? undefined,
    isDefault: m.is_default,
  };
}

/**
 * Fallback model list used while the API is loading or unavailable.
 * Should be kept roughly in sync with what production offers, but the
 * API response is always authoritative.
 */
export const FALLBACK_MODEL_OPTIONS: ModelOption[] = [
  { value: 'gpt-4o', label: 'GPT-4o', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'gpt-5.4-mini', label: 'GPT-5.4 Mini', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'gpt-5.4-nano', label: 'GPT-5.4 Nano', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'claude-sonnet-4.5', label: 'Claude Sonnet 4.5', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'claude-sonnet-4.6', label: 'Claude Sonnet 4.6', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'claude-haiku-4-5', label: 'Claude Haiku 4.5', provider: 'AWS Bedrock', supportsThinking: true },
  {
    value: 'gemini-3.1-pro-preview',
    label: 'Gemini 3.1 Pro Preview',
    provider: 'Google Vertex AI',
    supportsThinking: true,
  },
  {
    value: 'gemini-3-flash-preview',
    label: 'Gemini 3 Flash Preview',
    provider: 'Google Vertex AI',
    supportsThinking: true,
  },
  {
    value: 'gemini-3.1-flash-lite-preview',
    label: 'Gemini 3.1 Flash Lite Preview',
    provider: 'Google Vertex AI',
    supportsThinking: true,
  },
] as const;

/**
 * @deprecated Use useAvailableModels() hook instead.
 * Kept for backward compatibility — returns the static fallback list.
 */
export const MODEL_OPTIONS = FALLBACK_MODEL_OPTIONS;

/**
 * React Query hook to get the list of available models.
 * Returns the API response when available, falls back to FALLBACK_MODEL_OPTIONS.
 */
export function useAvailableModels() {
  const query = useQuery({
    ...listAvailableModelsApiV1ModelsGetOptions(),
    staleTime: 5 * 60 * 1000, // Cache for 5 minutes
    retry: 2,
  });

  return {
    models: query.data ? query.data.map(toModelOption) : FALLBACK_MODEL_OPTIONS,
    isLoading: query.isLoading,
    error: query.error,
  };
}

/**
 * Get model label by value.
 * Accepts an optional models list for dynamic lookups.
 */
export function getModelLabel(value: string, models?: ModelOption[]): string {
  return (models ?? FALLBACK_MODEL_OPTIONS).find((m) => m.value === value)?.label || value;
}

/**
 * Get model option by value.
 * Accepts an optional models list for dynamic lookups.
 */
export function getModelOption(value: string, models?: ModelOption[]): ModelOption | undefined {
  return (models ?? FALLBACK_MODEL_OPTIONS).find((m) => m.value === value);
}

/**
 * Check if a model supports extended thinking mode.
 * Accepts an optional models list for dynamic lookups.
 */
export function modelSupportsThinking(modelValue: string | null | undefined, models?: ModelOption[]): boolean {
  if (!modelValue) return false;
  return (models ?? FALLBACK_MODEL_OPTIONS).find((m) => m.value === modelValue)?.supportsThinking || false;
}

/**
 * Thinking level options for extended thinking configuration.
 */
export const THINKING_LEVEL_OPTIONS = [
  { value: 'minimal', label: 'Minimal', description: 'Quick responses with minimal thinking' },
  { value: 'low', label: 'Low', description: 'Balanced thinking for most tasks' },
  { value: 'medium', label: 'Medium', description: 'Deeper reasoning for complex tasks' },
  { value: 'high', label: 'High', description: 'Maximum reasoning depth' },
] as const;

/**
 * Get available thinking levels for a specific model.
 *
 * Uses the model's thinkingLevels field from the API if available,
 * otherwise falls back to hardcoded restrictions:
 * - Gemini 3.1 Pro: only supports 'low', 'medium' and 'high'
 * - All others: supports all levels
 */
export function getAvailableThinkingLevels(modelValue: string | null | undefined, models?: ModelOption[]) {
  const model = modelValue ? (models ?? FALLBACK_MODEL_OPTIONS).find((m) => m.value === modelValue) : undefined;

  // If the API provided explicit thinking levels, use them
  if (model?.thinkingLevels) {
    return THINKING_LEVEL_OPTIONS.filter((opt) => model.thinkingLevels!.includes(opt.value));
  }

  // Fallback: Gemini 3.1 Pro only supports low, medium and high
  if (modelValue === 'gemini-3.1-pro-preview') {
    return THINKING_LEVEL_OPTIONS.filter((opt) => opt.value === 'low' || opt.value === 'medium' || opt.value === 'high');
  }
  // All other models support all levels
  return THINKING_LEVEL_OPTIONS;
}
