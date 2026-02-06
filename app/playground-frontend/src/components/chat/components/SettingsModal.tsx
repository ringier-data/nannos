import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { toast } from 'sonner';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { AlertCircle, Sparkles } from 'lucide-react';
import { useChat } from '../contexts';
import { useSessionId } from '../hooks/useLocalStorage';
import type { Settings } from '../types';
import { MODEL_OPTIONS, modelSupportsThinking, getAvailableThinkingLevels } from '@/config/models';
import { config } from '@/config';

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

const AGENT_URL_OPTIONS = [
  { value: config.orchestratorUrl, label: config.orchestratorUrl },
  {
    value: 'https://sample-a2a-agent-908687846511.us-central1.run.app',
    label: 'https://sample-a2a-agent-908687846511.us-central1.run.app',
  },
  { value: 'custom', label: 'Custom URL...' },
];

export function SettingsModal({ isOpen, onClose }: SettingsModalProps) {
  const { settings, updateSettings } = useChat();
  const sessionId = useSessionId();

  const [agentUrlSelect, setAgentUrlSelect] = useState(
    AGENT_URL_OPTIONS.find((o) => o.value === settings?.agentUrl)?.value || 'custom'
  );
  const [customUrl, setCustomUrl] = useState(
    AGENT_URL_OPTIONS.find((o) => o.value === settings?.agentUrl) ? '' : settings?.agentUrl || ''
  );
  const [model, setModel] = useState(settings?.model || 'gpt4o');
  const [enableThinking, setEnableThinking] = useState(settings?.enableThinking || false);
  const [thinkingLevel, setThinkingLevel] = useState(settings?.thinkingLevel || 'low');
  const [isSaving, setIsSaving] = useState(false);

  // Reset form when modal opens
  useEffect(() => {
    if (isOpen && settings) {
      const isPreset = AGENT_URL_OPTIONS.find((o) => o.value === settings.agentUrl);
      if (isPreset) {
        setAgentUrlSelect(settings.agentUrl);
        setCustomUrl('');
      } else {
        setAgentUrlSelect('custom');
        setCustomUrl(settings.agentUrl || '');
      }
      setModel(settings.model || 'gpt4o');
      setEnableThinking(settings.enableThinking || false);
      setThinkingLevel(settings.thinkingLevel || 'low');
    }
  }, [isOpen, settings]);

  // Auto-reset thinking level when model changes if current level is not available
  useEffect(() => {
    const availableLevels = getAvailableThinkingLevels(model);
    if (!availableLevels.find((opt) => opt.value === thinkingLevel)) {
      setThinkingLevel(availableLevels[0]?.value || 'low');
    }
  }, [model, thinkingLevel]);

  const handleSave = async () => {
    const agentUrl = agentUrlSelect === 'custom' ? customUrl.trim() : agentUrlSelect;

    if (!agentUrl) {
      toast.error('Validation Error', { description: 'Agent URL is required' });
      return;
    }

    try {
      new URL(agentUrl);
    } catch {
      toast.error('Validation Error', { description: 'Invalid URL format. Please enter a valid URL (e.g., https://example.com)' });
      return;
    }

    setIsSaving(true);

    try {
      const newSettings: Settings = { 
        agentUrl, 
        model,
        enableThinking,
        thinkingLevel
      };
      const success = await updateSettings(newSettings);
      if (success) {
        toast.success('Settings saved successfully');
        onClose();
      } else {
        toast.error('Connection Error', { description: 'Failed to connect to agent. Please check the URL and try again.' });
      }
    } catch (e) {
      toast.error('Error', { description: `Failed to save settings: ${e instanceof Error ? e.message : 'Unknown error'}` });
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Connection Settings</DialogTitle>
        </DialogHeader>

        <div className="space-y-4 py-4">
          {/* Agent URL */}
          <div className="space-y-2">
            <Label htmlFor="agentUrl">
              Agent URL <span className="text-destructive">*</span>
            </Label>
            <Select value={agentUrlSelect} onValueChange={setAgentUrlSelect}>
              <SelectTrigger id="agentUrl">
                <SelectValue placeholder="Select agent URL" />
              </SelectTrigger>
              <SelectContent>
                {AGENT_URL_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {agentUrlSelect === 'custom' && (
              <Input
                type="text"
                value={customUrl}
                onChange={(e) => setCustomUrl(e.target.value)}
                placeholder="Enter custom URL"
                className="mt-2"
              />
            )}
            <p className="text-xs text-muted-foreground">Enter the A2A agent server URL to connect to</p>
          </div>

          {/* Model Selection */}
          <div className="space-y-2">
            <Label htmlFor="modelSelect">Model</Label>
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger id="modelSelect">
                <SelectValue placeholder="Select model" />
              </SelectTrigger>
              <SelectContent>
                {MODEL_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">Select the LLM model for the orchestrator to use</p>
          </div>

          {/* Extended Thinking Configuration */}
          {modelSupportsThinking(model) && (
            <div className="space-y-3 pt-2 border-t">
              <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <Label htmlFor="enableThinking" className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4" />
                    Extended Thinking
                  </Label>
                  <p className="text-xs text-muted-foreground">
                    Enable extended thinking for complex reasoning
                  </p>
                </div>
                <Switch
                  id="enableThinking"
                  checked={enableThinking}
                  onCheckedChange={setEnableThinking}
                />
              </div>

              {enableThinking && (
                <>
                  <div className="space-y-2">
                    <Label htmlFor="thinkingLevel">Thinking Level</Label>
                    <Select value={thinkingLevel} onValueChange={setThinkingLevel}>
                      <SelectTrigger id="thinkingLevel">
                        <SelectValue placeholder="Select thinking level" />
                      </SelectTrigger>
                      <SelectContent>
                        {getAvailableThinkingLevels(model).map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            <div className="flex flex-col">
                              <span>{option.label}</span>
                              <span className="text-xs text-muted-foreground">{option.description}</span>
                            </div>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  {(thinkingLevel === 'medium' || thinkingLevel === 'high') && (
                    <Alert>
                      <AlertCircle className="h-4 w-4" />
                      <AlertDescription className="text-xs">
                        Higher thinking levels increase response time and costs.
                      </AlertDescription>
                    </Alert>
                  )}
                </>
              )}
            </div>
          )}
        </div>

        <DialogFooter className="flex items-center justify-between sm:justify-between">
          <div className="text-xs text-muted-foreground" title={`Full Session ID: ${sessionId}`}>
            Session: {sessionId.slice(0, 8)}...
          </div>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={isSaving}>
              {isSaving ? 'Saving...' : 'Save'}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
