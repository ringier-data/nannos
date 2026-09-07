/**
 * Turning an agent action's form fields into an API body, and checking they are complete.
 *
 * Four call sites need this — create and edit, for a task job and for a watch whose
 * outcome is an agent. The two watch paths were silently omitting the inline definition,
 * so a sub-agent defined in the form was accepted and then dropped on submit, leaving the
 * job notify-only with no error.
 */
import type { AgentAction } from '@/components/AgentActionFields';
import type { AutomatedSubAgentConfig } from '@/api/scheduler';
import { isModelTier, modelTierOf } from '@/config/models';
import { config } from '@/config';

/** The API body an inline ('automated') definition becomes. */
export function automatedSubAgentParameters(value: AgentAction): AutomatedSubAgentConfig {
  return {
    name: value.automated_name.trim(),
    description: value.automated_description.trim(),
    // Exactly one of model / model_tier (the backend validates the XOR).
    model: isModelTier(value.automated_model) ? undefined : value.automated_model,
    model_tier: (modelTierOf(value.automated_model) ??
      undefined) as AutomatedSubAgentConfig['model_tier'],
    system_prompt: value.automated_system_prompt.trim(),
    mcp_tools: value.automated_mcp_tools.length > 0 ? value.automated_mcp_tools : null,
    enable_thinking: value.automated_enable_thinking || null,
    thinking_level: value.automated_enable_thinking ? value.automated_thinking_level : null,
  };
}

/**
 * The message for the first unmet requirement of an agent action, or null when it is
 * ready to submit. One list, so a watch and a task hold an inline definition to the
 * same standard.
 */
export function agentActionError(value: AgentAction): string | null {
  if (value.sub_agent_mode === 'existing') {
    return value.sub_agent_id ? null : 'Sub-agent is required';
  }
  if (!value.automated_name.trim()) return 'Sub-agent name is required';
  if (!value.automated_description.trim()) return 'Sub-agent description is required';
  if (!value.automated_model) return 'Model is required';
  if (!value.automated_system_prompt.trim()) return 'System prompt is required';
  if (value.automated_system_prompt.length > config.autoApprove.maxSystemPromptLength)
    return `System prompt must be ${config.autoApprove.maxSystemPromptLength} characters or less`;
  if (value.automated_description.length > 200)
    return 'Description must be 200 characters or less';
  if (value.automated_mcp_tools.length > config.autoApprove.maxMcpToolsCount)
    return `Maximum ${config.autoApprove.maxMcpToolsCount} MCP tools allowed`;
  return null;
}

