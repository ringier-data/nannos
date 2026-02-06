import { useState, useEffect, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Save, Loader2, Settings as SettingsIcon, Shield, Bot, Wrench, Globe, Key } from 'lucide-react';
import { toast } from 'sonner';
import {
  getCurrentUserSettingsApiV1AuthMeSettingsGetOptions,
  updateCurrentUserSettingsApiV1AuthMeSettingsPatchMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { OrchestratorThinkingLevel } from '@/api/generated/types.gen';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { SubAgentActivationList } from '@/components/settings/SubAgentActivationList';
import { UserPermissionsTable } from '@/components/settings/UserPermissionsTable';
import { MCPToolToggleList } from '@/components/settings/MCPToolToggleList';
import { SecretsVaultList } from '@/components/settings/SecretsVaultList';
import { ExtendedThinkingConfig } from '@/components/settings/ExtendedThinkingConfig';
import { MODEL_OPTIONS, modelSupportsThinking, getAvailableThinkingLevels } from '@/config/models';

const LANGUAGE_OPTIONS = [
  { value: 'en', label: 'English' },
  { value: 'de', label: 'Deutsch' },
  { value: 'fr', label: 'Français' },
];

type TabId = 'preferences' | 'vault' | 'permissions' | 'subagents' | 'tools';

interface Tab {
  id: TabId;
  label: string;
  icon: typeof SettingsIcon;
}

const tabs: Tab[] = [
  { id: 'preferences', label: 'Preferences', icon: SettingsIcon },
  { id: 'subagents', label: 'Sub-Agents', icon: Bot },
  { id: 'tools', label: 'MCP Tools', icon: Wrench },
  { id: 'vault', label: 'Secrets Vault', icon: Key },
  { id: 'permissions', label: 'Permissions', icon: Shield },
];

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<TabId>('preferences');
  const [language, setLanguage] = useState<string>('en');
  const [timezone, setTimezone] = useState<string>('Europe/Zurich');
  const [customPrompt, setCustomPrompt] = useState<string>('');
  const [mcpTools, setMcpTools] = useState<string[]>([]);
  const [preferredModel, setPreferredModel] = useState<string | null>(null);
  const [enableThinking, setEnableThinking] = useState<boolean>(false);
  const [thinkingLevel, setThinkingLevel] = useState<OrchestratorThinkingLevel | null>(null);
  const [hasChanges, setHasChanges] = useState(false);

  const { data: settingsData, isLoading } = useQuery({
    ...getCurrentUserSettingsApiV1AuthMeSettingsGetOptions(),
  });

  const settings = settingsData?.data;

  // Initialize form when data loads
  useEffect(() => {
    if (settings) {
      setLanguage(settings.language ?? 'en');
      setTimezone(settings.timezone ?? 'Europe/Zurich');
      setCustomPrompt(settings.custom_prompt ?? '');
      setMcpTools(settings.mcp_tools ?? []);
      setPreferredModel(settings.preferred_model ?? null);
      setEnableThinking(settings.enable_thinking ?? false);
      setThinkingLevel(settings.enable_thinking ? (settings.thinking_level ?? 'low') : null);
      setHasChanges(false);
    }
  }, [settings]);

  const updateMutation = useMutation({
    ...updateCurrentUserSettingsApiV1AuthMeSettingsPatchMutation(),
    onSuccess: () => {
      toast.success('Settings saved');
      queryClient.invalidateQueries({ queryKey: ['getCurrentUserSettingsApiV1AuthMeSettingsGet'] });
      setHasChanges(false);
    },
    onError: () => {
      toast.error('Failed to save settings');
    },
  });

  const handleLanguageChange = (value: string) => {
    setLanguage(value);
    setHasChanges(true);
  };

  const handleTimezoneChange = (value: string) => {
    setTimezone(value);
    setHasChanges(true);
  };

  const handleCustomPromptChange = (value: string) => {
    setCustomPrompt(value);
    setHasChanges(true);
  };

  const handleMcpToolsChange = (tools: string[]) => {
    setMcpTools(tools);
    setHasChanges(true);
  };

  const handlePreferredModelChange = (value: string | null) => {
    setPreferredModel(value);
    // Auto-reset thinking if new model doesn't support it
    if (value && !modelSupportsThinking(value)) {
      setEnableThinking(false);
      setThinkingLevel(null);
    }
    // Auto-reset thinking level if not available for new model
    if (value && enableThinking) {
      const availableLevels = getAvailableThinkingLevels(value);
      if (!availableLevels.find(opt => opt.value === thinkingLevel)) {
        setThinkingLevel(availableLevels[0]?.value || 'low');
      }
    }
    setHasChanges(true);
  };

  const handleEnableThinkingChange = (checked: boolean) => {
    setEnableThinking(checked);
    // Reset thinking level when disabling thinking
    if (!checked) {
      setThinkingLevel(null);
    } else if (thinkingLevel === null) {
      // Set default when enabling
      setThinkingLevel('low');
    }
    setHasChanges(true);
  };

  const handleThinkingLevelChange = (value: string) => {
    setThinkingLevel(value as OrchestratorThinkingLevel);
    setHasChanges(true);
  };

  // Get all available IANA timezones
  const TIMEZONE_OPTIONS = useMemo(() => {
    try {
      const timezones = Intl.supportedValuesOf('timeZone');
      return timezones.map((tz) => ({
        value: tz,
        label: tz.replace(/_/g, ' '),
      }));
    } catch {
      // Fallback for older browsers
      return [
        { value: 'Europe/Zurich', label: 'Europe/Zurich' },
        { value: 'America/New_York', label: 'America/New York' },
        { value: 'America/Los_Angeles', label: 'America/Los Angeles' },
        { value: 'Europe/London', label: 'Europe/London' },
        { value: 'Europe/Berlin', label: 'Europe/Berlin' },
        { value: 'Asia/Tokyo', label: 'Asia/Tokyo' },
        { value: 'UTC', label: 'UTC' },
      ];
    }
  }, []);

  const handleSave = () => {
    updateMutation.mutate({
      body: {
        language,
        timezone,
        custom_prompt: customPrompt || null,
        mcp_tools: mcpTools,
        preferred_model: preferredModel,
        enable_thinking: enableThinking,
        thinking_level: thinkingLevel,
      },
    });
  };

  if (isLoading) {
    return (
      <div className="space-y-6 p-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
          <p className="text-muted-foreground">Manage your preferences</p>
        </div>
        <Card>
          <CardHeader>
            <Skeleton className="h-6 w-32" />
            <Skeleton className="h-4 w-48" />
          </CardHeader>
          <CardContent className="space-y-4">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-32 w-full" />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 p-4 pb-8">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-muted-foreground">Manage your preferences and permissions</p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === tab.id
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground hover:border-muted-foreground/50'
            }`}
          >
            <tab.icon className="h-4 w-4" />
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      {activeTab === 'preferences' && (
        <div className="space-y-6">
          <div className="space-y-4 pb-4 border-b">
            <h3 className="text-lg font-semibold">Model Preferences</h3>
            
            <div className="space-y-2">
              <Label htmlFor="preferred-model">Preferred Model</Label>
              <Select value={preferredModel || 'default'} onValueChange={(val) => handlePreferredModelChange(val === 'default' ? null : val)}>
                <SelectTrigger id="preferred-model" className="w-full max-w-xs">
                  <SelectValue placeholder="Use default model" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">Use default (determined by agent)</SelectItem>
                  {MODEL_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">
                Set your preferred LLM model for the orchestrator. Leave as default to use agent-specific configuration.
              </p>
            </div>

            <ExtendedThinkingConfig
              model={preferredModel}
              enableThinking={enableThinking}
              thinkingLevel={thinkingLevel}
              onEnableThinkingChange={handleEnableThinkingChange}
              onThinkingLevelChange={handleThinkingLevelChange}
            />
          </div>

          <div className="space-y-4">
            <h3 className="text-lg font-semibold">General Preferences</h3>
          
          <div className="space-y-2">
            <Label htmlFor="language">Language</Label>
            <Select value={language} onValueChange={handleLanguageChange}>
              <SelectTrigger id="language" className="w-full max-w-xs">
                <SelectValue placeholder="Select language" />
              </SelectTrigger>
              <SelectContent>
                {LANGUAGE_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              Select the language the AI agent should use when responding.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="timezone" className="flex items-center gap-2">
              <Globe className="h-4 w-4" />
              Timezone
            </Label>
            <Select value={timezone} onValueChange={handleTimezoneChange}>
              <SelectTrigger id="timezone" className="w-full max-w-xs">
                <SelectValue placeholder="Select timezone" />
              </SelectTrigger>
              <SelectContent className="max-h-[300px]">
                {TIMEZONE_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              Select your timezone for accurate time-based queries (e.g., "tomorrow", "next week").
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="custom-prompt">Custom Prompt</Label>
            <Textarea
              id="custom-prompt"
              placeholder="Enter a custom prompt that will be used in your conversations..."
              value={customPrompt}
              onChange={(e) => handleCustomPromptChange(e.target.value)}
              rows={4}
              className="resize-none"
            />
            <p className="text-sm text-muted-foreground">
              Add a custom prompt that will be prepended to your conversations with AI agents.
            </p>
          </div>
          </div>

          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={!hasChanges || updateMutation.isPending}>
              {updateMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Save className="h-4 w-4 mr-2" />
              )}
              Save Changes
            </Button>
          </div>
        </div>
      )}

      {activeTab === 'vault' && (
        <div className="flex flex-col gap-6 max-h-[calc(100vh-16rem)] overflow-hidden">
          <div>
            <h2 className="text-lg font-semibold">Secrets Vault</h2>
            <p className="text-sm text-muted-foreground mt-1">
              Manage secure credentials and secrets for your sub-agents.
            </p>
          </div>
          <div className="flex-1 overflow-y-auto min-h-0">
            <SecretsVaultList />
          </div>
        </div>
      )}

      {activeTab === 'tools' && (
        <div className="flex flex-col gap-6 max-h-[calc(100vh-16rem)] overflow-hidden">
          <div>
            <h2 className="text-lg font-semibold">MCP Tools</h2>
            <p className="text-sm text-muted-foreground mt-1">
              Enable or disable MCP tools available to the orchestrator agent.
            </p>
          </div>
          <div className="flex-1 overflow-y-auto min-h-0">
            <MCPToolToggleList
              value={mcpTools}
              onChange={handleMcpToolsChange}
              disabled={updateMutation.isPending}
            />
          </div>
          <div className="flex justify-end border-t pt-4 bg-background">
            <Button onClick={handleSave} disabled={!hasChanges || updateMutation.isPending}>
              {updateMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Save className="h-4 w-4 mr-2" />
              )}
              Save Changes
            </Button>
          </div>
        </div>
      )}

      {activeTab === 'permissions' && <UserPermissionsTable />}

      {activeTab === 'subagents' && (
        <div className="flex flex-col gap-6 max-h-[calc(100vh-16rem)] overflow-hidden">
          <div>
            <h2 className="text-lg font-semibold">Sub-Agents</h2>
            <p className="text-sm text-muted-foreground mt-1">
              Activate or deactivate sub-agents available to the orchestrator.
            </p>
          </div>
          <div className="flex-1 overflow-y-auto min-h-0">
            <SubAgentActivationList />
          </div>
        </div>
      )}
    </div>
  );
}
