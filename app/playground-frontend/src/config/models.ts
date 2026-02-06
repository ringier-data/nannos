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
  supportsThinking?: boolean; // Indicates if model supports extended thinking mode
}

/**
 * Available LLM models for local agents and orchestrator.
 * 
 * Model values must match the backend's expected model identifiers.
 * supportsThinking indicates which models support extended thinking mode.
 */
export const MODEL_OPTIONS: ModelOption[] = [
  { value: 'gpt4o', label: 'GPT-4o', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini', provider: 'Azure OpenAI', supportsThinking: false },
  { value: 'claude-sonnet-4.5', label: 'Claude Sonnet 4.5', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'claude-haiku-4-5', label: 'Claude Haiku 4.5', provider: 'AWS Bedrock', supportsThinking: true },
  { value: 'gemini-3-pro-preview', label: 'Gemini 3 Pro Preview', provider: 'Google Vertex AI', supportsThinking: true },
  { value: 'gemini-3-flash-preview', label: 'Gemini 3 Flash Preview', provider: 'Google Vertex AI', supportsThinking: true },
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

/**
 * Check if a model supports extended thinking mode.
 */
export function modelSupportsThinking(modelValue: string | null | undefined): boolean {
  if (!modelValue) return false;
  return MODEL_OPTIONS.find(m => m.value === modelValue)?.supportsThinking || false;
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
 * According to Gemini 3 documentation:
 * - Gemini 3 Pro: only supports 'low' and 'high'
 * - Gemini 3 Flash: supports all levels
 * - Claude Sonnet: supports all levels
 */
export function getAvailableThinkingLevels(modelValue: string | null | undefined) {
  // Gemini 3 Pro only supports low and high
  if (modelValue === 'gemini-3-pro-preview') {
    return THINKING_LEVEL_OPTIONS.filter(opt => opt.value === 'low' || opt.value === 'high');
  }
  // All other models support all levels
  return THINKING_LEVEL_OPTIONS;
}
