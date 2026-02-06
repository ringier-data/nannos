import { useState, useEffect } from 'react';
import { Link } from 'react-router';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { toast } from 'sonner';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { AlertCircle, Sparkles, Info } from 'lucide-react';
import { useChat } from '../contexts';
import { useSessionId } from '../hooks/useLocalStorage';
import type { Settings } from '../types';
import { MODEL_OPTIONS, modelSupportsThinking, getAvailableThinkingLevels } from '@/config/models';
import { config } from '@/config';

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function SettingsModal({ isOpen, onClose }: SettingsModalProps) {
  const { settings, userSettings, updateSettings } = useChat();
  const sessionId = useSessionId();

  // Check if user has configured settings in the Settings page
  // User has settings if preferred_model is explicitly set (not null)
  const hasUserSettings = userSettings?.preferred_model !== null && userSettings?.preferred_model !== undefined;

  const [model, setModel] = useState(settings?.model || 'gpt4o');
  const [enableThinking, setEnableThinking] = useState(settings?.enableThinking || false);
  const [thinkingLevel, setThinkingLevel] = useState(settings?.thinkingLevel || 'low');
  const [isSaving, setIsSaving] = useState(false);

  // Reset form when modal opens
  useEffect(() => {
    if (isOpen && settings) {
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
    setIsSaving(true);

    try {
      // Keep existing agentUrl from settings or use default
      const agentUrl = settings?.agentUrl || config.orchestratorUrl;
      
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
        toast.error('Connection Error', { description: 'Failed to save settings.' });
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
          {/* Info Alert */}
          {hasUserSettings ? (
            <Alert>
              <Info className="h-4 w-4" />
              <AlertTitle>Settings Controlled by User Preferences</AlertTitle>
              <AlertDescription className="text-xs space-y-1">
                <p>You have configured your preferences in the Settings page, which control your chat experience.</p>
                <p>
                  To modify these settings, visit the{' '}
                  <Link to="/app" className="font-medium underline hover:text-primary">
                    Settings page
                  </Link>
                  .
                </p>
              </AlertDescription>
            </Alert>
          ) : (
            <>
              <Alert>
                <Info className="h-4 w-4" />
                <AlertTitle>Temporary Client Settings</AlertTitle>
                <AlertDescription className="text-xs space-y-1">
                  <p>Configure temporary settings for this chat session.</p>
                  <p>
                    For persistent settings, configure them in the{' '}
                    <Link to="/app" className="font-medium underline hover:text-primary">
                      Settings page
                    </Link>
                    .
                  </p>
                </AlertDescription>
              </Alert>

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
            </>
          )}
        </div>

        <DialogFooter className="flex items-center justify-between sm:justify-between">
          <div className="text-xs text-muted-foreground" title={`Full Session ID: ${sessionId}`}>
            Session: {sessionId.slice(0, 8)}...
          </div>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={onClose}>
              {hasUserSettings ? 'Close' : 'Cancel'}
            </Button>
            {!hasUserSettings && (
              <Button onClick={handleSave} disabled={isSaving}>
                {isSaving ? 'Saving...' : 'Save'}
              </Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
