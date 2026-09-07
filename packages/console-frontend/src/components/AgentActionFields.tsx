/**
 * "What should run" — the fields that define an agent action.
 *
 * Task jobs and watch jobs reach this point by different routes (a schedule firing
 * versus a condition being met), but once the trigger is settled the thing being
 * configured is identical: which sub-agent, or one defined inline, and what to tell
 * it. Presenting two different forms for that was an accident of how the page grew.
 *
 * Voice call is deliberately not here. It decides how the outcome reaches the user, not
 * what runs, so it lives with the delivery channel — and it applies to a watch that only
 * notifies just as much as to one that runs an agent.
 */
import { useEffect } from 'react';

import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { SubAgentSelect } from '@/components/SubAgentSelect';
import { McpToolSelect } from '@/components/McpToolSelect';
import {
  useAvailableModels,
  modelSelectOptions,
  isModelTier,
  MODEL_TIER_OPTIONS,
} from '@/config/models';
import { config } from '@/config';
import type { McpTool } from '@/api/generated/types.gen';

/** The subset of a job form that describes what to run. */
export interface AgentAction {
  sub_agent_mode: 'existing' | 'automated';
  sub_agent_id: string;
  automated_name: string;
  automated_description: string;
  automated_model: string;
  automated_system_prompt: string;
  automated_mcp_tools: string[];
  automated_enable_thinking: boolean;
  automated_thinking_level: string;
  prompt: string;
}

export function AgentActionFields({
  value,
  onChange,
  subAgents,
  mcpTools,
  instructionLabel = 'Instruction',
  instructionPlaceholder,
  instructionHint,
  onLimitExceeded,
  fieldErrors,
}: {
  value: AgentAction;
  onChange: (patch: Partial<AgentAction>) => void;
  subAgents: { id: number; name: string; type?: string | null }[];
  mcpTools: McpTool[];
  instructionLabel?: string;
  instructionPlaceholder?: string;
  instructionHint?: string;
  /** Called when the tool cap is hit, so the page can surface it its own way. */
  onLimitExceeded?: (message: string) => void;
  fieldErrors?: Record<string, string>;
}) {
  const { models: availableModels } = useAvailableModels();
  const tierSelected = isModelTier(value.automated_model);

  // A tier always resolves (it follows the fleet default), so only a pinned alias can
  // go stale. If the selected alias is no longer registered on the gateway, fall back
  // to the standard tier rather than rendering an empty picker or submitting a dead
  // model. Deferred to a microtask so the parent's state update is not a synchronous
  // cascade out of this render.
  useEffect(() => {
    if (tierSelected || availableModels.length === 0) return;
    if (availableModels.some((m) => m.value === value.automated_model)) return;
    const timer = setTimeout(() => onChange({ automated_model: 'tier:standard' }), 0);
    return () => clearTimeout(timer);
    // onChange is a fresh closure each render; re-running on it would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availableModels, value.automated_model, tierSelected]);
  const modelAlias = tierSelected ? '' : value.automated_model;
  const thinkingCapable =
    value.automated_model.startsWith('claude') || value.automated_model.startsWith('gemini');

  return (
    <>
      <div className="grid gap-1.5">
        <Label>Sub-agent</Label>
        <Select
          value={value.sub_agent_mode}
          onValueChange={(v) => onChange({ sub_agent_mode: v as AgentAction['sub_agent_mode'] })}
        >
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="existing">Use an existing sub-agent</SelectItem>
            <SelectItem value="automated">Define one inline</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {value.sub_agent_mode === 'existing' ? (
        <div className="grid gap-1.5">
          {/* No "None" entry: reaching this panel already means an agent runs. */}
          <SubAgentSelect
            value={value.sub_agent_id}
            onChange={(v) => onChange({ sub_agent_id: v })}
            subAgents={subAgents}
          />
          {fieldErrors?.sub_agent_id && (
            <span className="text-destructive text-xs">{fieldErrors.sub_agent_id}</span>
          )}
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-1.5">
              <Label htmlFor="automated_name">Name</Label>
              <Input
                id="automated_name"
                value={value.automated_name}
                onChange={(e) => onChange({ automated_name: e.target.value })}
                placeholder="My Automated Agent"
                aria-invalid={Boolean(fieldErrors?.automated_name) || undefined}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="automated_model">Model</Label>
              <Select
                value={value.automated_model}
                onValueChange={(v) => onChange({ automated_model: v })}
              >
                <SelectTrigger id="automated_model">
                  <SelectValue placeholder="Select a model or tier" />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectLabel>Tier (follows the fleet default for that tier)</SelectLabel>
                    {MODEL_TIER_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                  <SelectSeparator />
                  <SelectGroup>
                    <SelectLabel>Specific model</SelectLabel>
                    {modelSelectOptions(modelAlias, availableModels, false).options.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
              {tierSelected && (
                <p className="text-muted-foreground text-xs">
                  Runs on whichever model is the current default for this tier — survives model
                  upgrades.
                </p>
              )}
            </div>
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="automated_description">
              Description <span className="text-muted-foreground text-xs">(max 200 chars)</span>
            </Label>
            <Input
              id="automated_description"
              value={value.automated_description}
              onChange={(e) => onChange({ automated_description: e.target.value })}
              placeholder="Short description of the agent's skill"
              maxLength={200}
              aria-invalid={Boolean(fieldErrors?.automated_description) || undefined}
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="automated_system_prompt">
              System prompt{' '}
              <span className="text-muted-foreground text-xs">
                (max {config.autoApprove.maxSystemPromptLength} chars)
              </span>
            </Label>
            <Textarea
              id="automated_system_prompt"
              rows={4}
              value={value.automated_system_prompt}
              onChange={(e) => onChange({ automated_system_prompt: e.target.value })}
              placeholder="System prompt describing the task for the agent…"
              maxLength={config.autoApprove.maxSystemPromptLength}
              aria-invalid={Boolean(fieldErrors?.automated_system_prompt) || undefined}
            />
          </div>

          <div className="grid gap-1.5">
            <Label>
              MCP tools{' '}
              <span className="text-muted-foreground text-xs">
                (max {config.autoApprove.maxMcpToolsCount}, optional)
              </span>
            </Label>
            <McpToolSelect
              tools={mcpTools}
              values={value.automated_mcp_tools}
              placeholder="Add MCP tools…"
              onToggle={(toolName) => {
                const current = value.automated_mcp_tools;
                if (current.includes(toolName)) {
                  onChange({ automated_mcp_tools: current.filter((t) => t !== toolName) });
                } else if (current.length < config.autoApprove.maxMcpToolsCount) {
                  onChange({ automated_mcp_tools: [...current, toolName] });
                } else {
                  onLimitExceeded?.(
                    `Maximum ${config.autoApprove.maxMcpToolsCount} MCP tools allowed`,
                  );
                }
              }}
            />
            {value.automated_mcp_tools.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {value.automated_mcp_tools.map((tool) => (
                  <Badge key={tool} variant="secondary" className="gap-1">
                    {tool}
                    <button
                      type="button"
                      className="hover:text-destructive ml-1"
                      onClick={() =>
                        onChange({
                          automated_mcp_tools: value.automated_mcp_tools.filter((t) => t !== tool),
                        })
                      }
                    >
                      ×
                    </button>
                  </Badge>
                ))}
              </div>
            )}
          </div>

          {thinkingCapable && (
            <>
              <div className="flex items-center gap-2">
                <Switch
                  id="automated_enable_thinking"
                  checked={value.automated_enable_thinking}
                  onCheckedChange={(checked) => onChange({ automated_enable_thinking: checked })}
                />
                <Label htmlFor="automated_enable_thinking" className="cursor-pointer font-normal">
                  Enable extended thinking
                </Label>
              </div>
              {value.automated_enable_thinking && (
                <div className="grid gap-1.5">
                  <Label>Thinking level</Label>
                  <Select
                    value={value.automated_thinking_level}
                    onValueChange={(v) => onChange({ automated_thinking_level: v })}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {['minimal', 'low', 'medium', 'high'].map((level) => (
                        <SelectItem key={level} value={level}>
                          {level[0].toUpperCase() + level.slice(1)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </>
          )}
        </>
      )}

      <div className="grid gap-1.5">
        <Label htmlFor="agent_prompt">
          {instructionLabel} <span className="text-muted-foreground text-xs">(optional)</span>
        </Label>
        <Textarea
          id="agent_prompt"
          rows={3}
          value={value.prompt}
          onChange={(e) => onChange({ prompt: e.target.value })}
          placeholder={instructionPlaceholder ?? 'Specific instruction for this execution…'}
        />
        {instructionHint && <p className="text-muted-foreground text-xs">{instructionHint}</p>}
      </div>
    </>
  );
}
