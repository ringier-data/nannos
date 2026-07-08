// Model catalog for the chat settings UI. Static fallback + pure helpers,
// extracted from console-frontend's config/models.ts. The live list comes from
// the host via `adapter.api.listModels` (console injects its Model Gateway
// query); the static list keeps pickers rendering when the host provides none.

import { useEffect, useState } from 'react';
import { useHostAdapter } from '../adapter';

export interface ModelOption {
  value: string;
  label: string;
  provider?: string;
  supportsThinking?: boolean;
  thinkingLevels?: string[];
}

export const MODEL_OPTIONS: ModelOption[] = [
  { value: 'gpt-4o', label: 'GPT-4o', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'claude-sonnet-4.5', label: 'Claude Sonnet 4.5', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'claude-sonnet-4.6', label: 'Claude Sonnet 4.6', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'claude-haiku-4-5', label: 'Claude Haiku 4.5', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'gemini-3.1-pro-preview', label: 'Gemini 3.1 Pro Preview', provider: 'Google Vertex AI', supportsThinking: true },
  { value: 'gemini-3-flash-preview', label: 'Gemini 3 Flash Preview', provider: 'Google Vertex AI', supportsThinking: true },
];

export function getModelLabel(value: string, models?: ModelOption[]): string {
  return (models ?? MODEL_OPTIONS).find((m) => m.value === value)?.label || value;
}

export function modelSupportsThinking(modelValue: string | null | undefined, _models?: ModelOption[]): boolean {
  if (!modelValue) return false;
  const models = _models ?? MODEL_OPTIONS;
  return models.find((m) => m.value === modelValue)?.supportsThinking || false;
}

/** Reasoning-effort options (LiteLLM convention). */
export const THINKING_LEVEL_OPTIONS = [
  { value: 'minimal', label: 'Minimal', description: 'Fastest — least reasoning' },
  { value: 'low', label: 'Low', description: 'Balanced thinking for most tasks' },
  { value: 'medium', label: 'Medium', description: 'Deeper reasoning for complex tasks' },
  { value: 'high', label: 'High', description: 'Thorough reasoning' },
  { value: 'xhigh', label: 'Extra high', description: 'Maximum reasoning depth' },
] as const;

export function getAvailableThinkingLevels(modelValue: string | null | undefined, models?: ModelOption[]) {
  const supported = (models ?? MODEL_OPTIONS).find((m) => m.value === modelValue)?.thinkingLevels;
  if (supported && supported.length > 0) {
    return THINKING_LEVEL_OPTIONS.filter((opt) => supported.includes(opt.value));
  }
  return THINKING_LEVEL_OPTIONS;
}

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
 * Available models, live from the host when `adapter.api.listModels` is
 * provided; static fallback otherwise (or on error / empty list).
 */
export function useAvailableModels(): { models: ModelOption[]; isLoading: boolean } {
  const adapter = useHostAdapter();
  const [models, setModels] = useState<ModelOption[]>(MODEL_OPTIONS);
  const [isLoading, setIsLoading] = useState(!!adapter.api.listModels);

  useEffect(() => {
    const listModels = adapter.api.listModels;
    if (!listModels) return;
    let cancelled = false;
    listModels()
      .then((live) => {
        if (!cancelled && live && live.length > 0) setModels(live);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [adapter.api.listModels]);

  return { models, isLoading };
}
