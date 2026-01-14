/**
 * Model configuration for the application.
 * 
 * This file is the single source of truth for available LLM models.
 * Update this file to add/remove models across the entire application.
 */

export interface ModelOption {
  value: string;
  label: string;
  provider?: string;
}

/**
 * Available LLM models for local agents and orchestrator.
 * 
 * Model values must match the backend's expected model identifiers.
 */
export const MODEL_OPTIONS: ModelOption[] = [
  { value: 'gpt4o', label: 'GPT-4o', provider: 'Azure OpenAI' },
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini', provider: 'Azure OpenAI' },
  { value: 'claude-sonnet-4.5', label: 'Claude Sonnet 4.5', provider: 'AWS Bedrock' },
  { value: 'claude-haiku-4-5', label: 'Claude Haiku 4.5', provider: 'AWS Bedrock' },
  { value: 'gemini-3-pro-preview', label: 'Gemini 3 Pro Preview', provider: 'Google Vertex AI' },
  { value: 'gemini-3-flash-preview', label: 'Gemini 3 Flash Preview', provider: 'Google Vertex AI' },
] as const;

/**
 * Get model label by value.
 */
export function getModelLabel(value: string): string {
  return MODEL_OPTIONS.find(m => m.value === value)?.label || value;
}

/**
 * Get model option by value.
 */
export function getModelOption(value: string): ModelOption | undefined {
  return MODEL_OPTIONS.find(m => m.value === value);
}
