import { AlertCircle } from 'lucide-react';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { useAvailableModels, modelSupportsThinking, getAvailableThinkingLevels } from '@/config/models';
import type { OrchestratorThinkingLevel } from '@/api/generated/types.gen';

interface ExtendedThinkingConfigProps {
  /**
   * Current model value - used to check thinking support and available levels
   */
  model: string | null | undefined;

  /**
   * Whether extended thinking is enabled
   */
  enableThinking: boolean | null;

  /**
   * Current thinking level
   */
  thinkingLevel: OrchestratorThinkingLevel | null;

  /**
   * Callback when thinking enable/disable changes
   */
  onEnableThinkingChange: (enabled: boolean) => void;

  /**
   * Callback when thinking level changes
   */
  onThinkingLevelChange: (level: OrchestratorThinkingLevel) => void;

  /**
   * Whether the inputs are disabled (e.g., during form submission)
   */
  disabled?: boolean;

  /**
   * Whether to show as a card with background (default: true)
   */
  showAsCard?: boolean;
}

export function ExtendedThinkingConfig({
  model,
  enableThinking,
  thinkingLevel,
  onEnableThinkingChange,
  onThinkingLevelChange,
  disabled = false,
  showAsCard = true,
}: ExtendedThinkingConfigProps) {
  const { models: availableModels } = useAvailableModels();
  const supportsThinking = modelSupportsThinking(model, availableModels);

  // Don't show anything if model doesn't support thinking
  if (!supportsThinking) {
    return null;
  }

  const containerClasses = showAsCard ? 'rounded-lg border bg-muted/50 p-4 space-y-4' : 'space-y-4';

  return (
    <div className={containerClasses}>
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1 flex-1">
          <Label htmlFor="enable-thinking" className="text-base font-medium cursor-pointer">
            Extended Thinking
          </Label>
          <p className="text-sm text-muted-foreground">Enable extended thinking for complex reasoning tasks</p>
        </div>
        <Switch
          id="enable-thinking"
          checked={enableThinking ?? false}
          onCheckedChange={onEnableThinkingChange}
          disabled={disabled}
          className="mt-1"
        />
      </div>

      {enableThinking && (
        <>
          <div className="space-y-2 pt-4 border-t">
            <Label htmlFor="thinking-level">Thinking Level</Label>
            <Select
              value={thinkingLevel || undefined}
              onValueChange={(value) => onThinkingLevelChange(value as OrchestratorThinkingLevel)}
              disabled={disabled}
            >
              <SelectTrigger id="thinking-level" className="w-full max-w-xs">
                <SelectValue placeholder="Select thinking level">
                  {thinkingLevel &&
                    getAvailableThinkingLevels(model, availableModels).find((opt) => opt.value === thinkingLevel)
                      ?.label}
                </SelectValue>
              </SelectTrigger>
              <SelectContent className="max-w-xs">
                {getAvailableThinkingLevels(model, availableModels).map((option) => (
                  <SelectItem key={option.value} value={option.value} className="cursor-pointer">
                    <div className="flex flex-col items-start gap-1 py-1">
                      <span className="font-medium">{option.label}</span>
                      <span className="text-xs text-muted-foreground leading-snug">{option.description}</span>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {(thinkingLevel === 'medium' || thinkingLevel === 'high') && (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription className="text-sm">
                Higher thinking levels increase response time and costs.
              </AlertDescription>
            </Alert>
          )}
        </>
      )}
    </div>
  );
}
