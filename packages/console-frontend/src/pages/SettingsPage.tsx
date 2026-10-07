import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Save, Loader2, Settings as SettingsIcon, Shield, Bot, Wrench, Globe, Key, Phone, X, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import {
  getCurrentUserApiV1AuthMeGetOptions,
  getCurrentUserApiV1AuthMeGetQueryKey,
  getCurrentUserSettingsApiV1AuthMeSettingsGetOptions,
  updateCurrentUserSettingsApiV1AuthMeSettingsPatchMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { OrchestratorThinkingLevel } from '@/api/generated/types.gen';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { SubAgentActivationList } from '@/components/settings/SubAgentActivationList';
import { UserPermissionsTable } from '@/components/settings/UserPermissionsTable';
import { MCPToolToggleList } from '@/components/settings/MCPToolToggleList';
import { SecretsVaultList } from '@/components/settings/SecretsVaultList';
import { ExtendedThinkingConfig } from '@/components/settings/ExtendedThinkingConfig';
import { PhoneVerificationDialog } from '@/components/settings/PhoneVerificationDialog';
import { ToolBypassRulesList } from '@/components/settings/ToolBypassRulesList';
import { NannosForm } from '@/components/nannos/NannosForm';
import type { ObjectAction, SubmitOutcome } from '@nannos/embed-sdk';
import { getErrorMessage } from '@/lib/utils';
import { useAvailableModels, modelSupportsThinking, getAvailableThinkingLevels, modelSelectOptions, getModelLabel } from '@/config/models';

const LANGUAGE_OPTIONS = [
  { value: 'en', label: 'English' },
  { value: 'de', label: 'Deutsch' },
  { value: 'fr', label: 'Français' },
];

type TabId = 'preferences' | 'vault' | 'permissions' | 'subagents' | 'tools' | 'approvals';

interface Tab {
  id: TabId;
  label: string;
  icon: typeof SettingsIcon;
}

const tabs: Tab[] = [
  { id: 'preferences', label: 'Preferences', icon: SettingsIcon },
  { id: 'subagents', label: 'Sub-Agents', icon: Bot },
  { id: 'tools', label: 'MCP Tools', icon: Wrench },
  { id: 'approvals', label: 'Tool Approvals', icon: ShieldCheck },
  { id: 'vault', label: 'Secrets Vault', icon: Key },
  { id: 'permissions', label: 'Permissions', icon: Shield },
];

const TAB_IDS = new Set<string>(tabs.map((t) => t.id));

function getTabFromHash(): TabId {
  const hash = window.location.hash.replace('#', '');
  return TAB_IDS.has(hash) ? (hash as TabId) : 'preferences';
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const { models: availableModels } = useAvailableModels();
  const [activeTab, setActiveTab] = useState<TabId>(getTabFromHash);
  const handleTabChange = useCallback((tab: TabId) => {
    setActiveTab(tab);
    window.location.hash = tab;
  }, []);

  useEffect(() => {
    const onHashChange = () => setActiveTab(getTabFromHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const [language, setLanguage] = useState<string>('en');
  const [timezone, setTimezone] = useState<string>('UTC');
  const [customPrompt, setCustomPrompt] = useState<string>('');
  const [mcpTools, setMcpTools] = useState<string[]>([]);
  const [preferredModel, setPreferredModel] = useState<string | null>(null);
  const [enableThinking, setEnableThinking] = useState<boolean | null>(null);
  const [thinkingLevel, setThinkingLevel] = useState<OrchestratorThinkingLevel | null>(null);
  const [verifyDialogOpen, setVerifyDialogOpen] = useState(false);
  const [phoneDraft, setPhoneDraft] = useState<string | undefined>();
  // The phone is not a form field: changing it means proving ownership by a code. The
  // assistant can start that (number prefilled); the user sends and enters the code.
  const changePhoneAction: ObjectAction = {
    label: 'Change phone number',
    description:
      'Open the phone verification dialog with this number filled in. Nothing changes until the user ' +
      'sends the code and enters it — tell them to.',
    params: [{ name: 'phone', type: 'string', description: 'E.164, e.g. +41791234567 (no spaces)' }],
    run: ({ phone }) => {
      const number = typeof phone === 'string' ? phone.replace(/[\s()-]/g, '') : '';
      if (!/^\+[1-9]\d{1,14}$/.test(number)) return { ok: false, detail: 'Not an E.164 number, e.g. +41791234567.' };
      setPhoneDraft(number);
      setVerifyDialogOpen(true);
    },
  };
  const [hasChanges, setHasChanges] = useState(false);

  const { data: settingsData, isLoading } = useQuery({
    ...getCurrentUserSettingsApiV1AuthMeSettingsGetOptions(),
  });

  const { data: userData } = useQuery({
    ...getCurrentUserApiV1AuthMeGetOptions(),
  });

  // Cast to access phone fields not yet in generated types
  const currentUser = userData as Record<string, unknown> | undefined;

  const settings = settingsData?.data;

  // Seed only while the form is clean: a background refetch (window focus, another
  // tab's save) must not wipe unsaved edits, the assistant's included. A ref, not a
  // dep, so flipping hasChanges after a save doesn't re-seed from the stale copy.
  const hasChangesRef = useRef(hasChanges);
  useEffect(() => {
    hasChangesRef.current = hasChanges;
  }, [hasChanges]);

  // Initialize form when data loads
  useEffect(() => {
    if (settings && !hasChangesRef.current) {
      setLanguage(settings.language ?? 'en');
      setTimezone(settings.timezone ?? 'UTC');
      setCustomPrompt(settings.custom_prompt ?? '');
      setMcpTools(settings.mcp_tools ?? []);
      setPreferredModel(settings.preferred_model ?? null);
      setEnableThinking(settings.enable_thinking ?? null);
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

    // When set to "default" (null), also reset thinking settings to null
    if (value === null) {
      setEnableThinking(null);
      setThinkingLevel(null);
    } else {
      // Auto-reset thinking if new model doesn't support it
      if (!modelSupportsThinking(value, availableModels)) {
        setEnableThinking(null);
        setThinkingLevel(null);
      }
      // Auto-reset thinking level if not available for new model
      if (enableThinking) {
        const availableLevels = getAvailableThinkingLevels(value, availableModels);
        if (!availableLevels.find((opt) => opt.value === thinkingLevel)) {
          setThinkingLevel(availableLevels[0]?.value || 'low');
        }
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

  // Assistant writes to the model/thinking trio. The agent may set all three in one
  // tick and in any order, so the requested values accumulate here and every write
  // re-derives the trio with the same rules the handlers above apply.
  const agentModelRef = useRef({ model: preferredModel, enable: enableThinking, level: thinkingLevel });
  useEffect(() => {
    agentModelRef.current = { model: preferredModel, enable: enableThinking, level: thinkingLevel };
  }, [preferredModel, enableThinking, thinkingLevel]);
  const writeAgentModel = (patch: Partial<typeof agentModelRef.current>) => {
    if (patch.model && !availableModels.some((m) => m.value === patch.model)) return;
    const requested = { ...agentModelRef.current, ...patch };
    // Picking a level is what turns thinking on, as in the level select.
    if (patch.level) requested.enable = true;
    agentModelRef.current = requested;
    const { model } = requested;
    let enable: boolean | null = null;
    let level: OrchestratorThinkingLevel | null = null;
    if (model !== null && modelSupportsThinking(model, availableModels)) {
      enable = requested.enable;
      if (enable) {
        const levels = getAvailableThinkingLevels(model, availableModels);
        level = levels.find((opt) => opt.value === requested.level)?.value ?? levels[0]?.value ?? 'low';
      }
    }
    setPreferredModel(model);
    setEnableThinking(enable);
    setThinkingLevel(level);
    setHasChanges(true);
  };

  // Get all available IANA timezones
  const TIMEZONE_OPTIONS = useMemo(() => {
    try {
      // Chrome's list has no "UTC" (nor any Etc/*), yet UTC is this page's own default
      // and a saved value: without it the dropdown shows blank for those users.
      const timezones = Intl.supportedValuesOf('timeZone');
      return [...(timezones.includes('UTC') ? [] : ['UTC']), ...timezones].map((tz) => ({
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

  const save = async (): Promise<SubmitOutcome> => {
    // When preferred_model is null (default), also send thinking settings as null to use agent defaults
    const shouldUseDefaults = preferredModel === null;

    try {
      await updateMutation.mutateAsync({
        body: {
          language,
          timezone,
          custom_prompt: customPrompt || null,
          mcp_tools: mcpTools,
          preferred_model: preferredModel,
          enable_thinking: shouldUseDefaults ? null : enableThinking,
          thinking_level: shouldUseDefaults ? null : thinkingLevel,

        },
      });
      return true;
    } catch (err) {
      // onError has already toasted.
      return { ok: false, detail: getErrorMessage(err) };
    }
  };

  const handleSave = () => {
    void save();
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
            onClick={() => handleTabChange(tab.id)}
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

      {/* Registered once the saved settings are in: an apply that lands earlier (a client
          action auto-settled right after a reload) was reported done, then overwritten by
          the data-load effect above. */}
      {settings && (activeTab === 'preferences' || activeTab === 'tools') && (
        <NannosForm
          type="Settings"
          id="me"
          dirty={hasChanges}
          actions={activeTab === 'preferences' ? { change_phone: changePhoneAction } : undefined}
          // Each tab offers the fields it shows; one save path persists them all.
          fields={
            activeTab === 'preferences'
              ? {
                  preferredModel: [preferredModel, (v: string | null) => writeAgentModel({ model: v })],
                  enableThinking: [enableThinking, (v: boolean | null) => writeAgentModel({ enable: v })],
                  thinkingLevel: [thinkingLevel, (v: OrchestratorThinkingLevel | null) => writeAgentModel({ level: v })],
                  language: [language, handleLanguageChange],
                  timezone: [
                    timezone,
                    (v: string) => {
                      if (TIMEZONE_OPTIONS.some((o) => o.value === v)) handleTimezoneChange(v);
                    },
                  ],
                  customPrompt: [customPrompt, handleCustomPromptChange],
                }
              : { mcpTools: [mcpTools, handleMcpToolsChange] }
          }
          submit={save}
        />
      )}

      {/* Tab Content */}
      {activeTab === 'preferences' && (
        <div className="space-y-6">
          <div className="space-y-4 pb-4 border-b">
            <h3 className="text-lg font-semibold">Model Preferences</h3>

            <div className="space-y-2">
              <Label htmlFor="preferred-model">Preferred Model</Label>
              <Select
                value={preferredModel || 'default'}
                onValueChange={(val) => handlePreferredModelChange(val === 'default' ? null : val)}
              >
                <SelectTrigger id="preferred-model" className="w-full max-w-xs">
                  <SelectValue placeholder="Use default model" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">Use default (determined by agent)</SelectItem>
                  {modelSelectOptions(preferredModel, availableModels, settings?.preferred_model_retired ?? false).options.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {settings?.preferred_model_retired && preferredModel === settings.preferred_model ? (
                <p className="text-sm text-amber-600 dark:text-amber-400">
                  This model was retired — the orchestrator now uses{' '}
                  {settings.effective_preferred_model ? getModelLabel(settings.effective_preferred_model, availableModels) : 'the default'}.
                  Pick a replacement to update your preference.
                </p>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Set your preferred LLM model for the orchestrator. Leave as default to use agent-specific configuration.
                </p>
              )}
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
            <Label htmlFor="phone-number" className="flex items-center gap-2">
              <Phone className="h-4 w-4" />
              Phone Number
            </Label>
            <div className="flex items-center gap-2 max-w-xs">
              {currentUser?.phone_number ? (
                <>
                  <span className="text-sm font-mono">{currentUser.phone_number as string}</span>
                  {currentUser?.phone_number_override && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          onClick={async () => {
                            try {
                              const { client } = await import('@/api/generated/client.gen');
                              await client.delete({ url: '/api/v1/auth/me/phone/override' });
                              toast.success('Phone number override removed');
                              queryClient.invalidateQueries({ queryKey: getCurrentUserApiV1AuthMeGetQueryKey() });
                            } catch {
                              toast.error('Failed to remove phone number override');
                            }
                          }}
                        >
                          <X className="h-3 w-3" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Remove override and use IdP number</TooltipContent>
                    </Tooltip>
                  )}
                  <Button variant="outline" size="sm" onClick={() => setVerifyDialogOpen(true)}>
                    Change
                  </Button>
                </>
              ) : (
                <Button variant="outline" size="sm" onClick={() => setVerifyDialogOpen(true)}>
                  Verify Phone Number
                </Button>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              Your phone number in E.164 format. Used by the voice agent to call you.
              {currentUser?.phone_number_idp ? (
                <span className="block mt-1 text-xs">
                  Synced from your identity provider: {String(currentUser.phone_number_idp)}
                  {currentUser.phone_number_override ? ' (overridden)' : ''}
                </span>
              ) : null}
            </p>
          </div>

          <PhoneVerificationDialog
            open={verifyDialogOpen}
            onOpenChange={(o) => {
              setVerifyDialogOpen(o);
              if (!o) setPhoneDraft(undefined);
            }}
            initialNumber={phoneDraft}
            onVerified={() => {
              queryClient.invalidateQueries({ queryKey: getCurrentUserApiV1AuthMeGetQueryKey() });
            }}
          />

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
              <br /> Mind that the <b>general-purpose agent</b> will have access to all the tools by default.
            </p>
          </div>
          <div className="flex-1 overflow-y-auto min-h-0">
            <MCPToolToggleList value={mcpTools} onChange={handleMcpToolsChange} disabled={updateMutation.isPending} />
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

      {activeTab === 'approvals' && (
        <div className="flex flex-col gap-6 max-h-[calc(100vh-16rem)] overflow-hidden">
          <div>
            <h2 className="text-lg font-semibold">Tool Approval Bypass Rules</h2>
            <p className="text-sm text-muted-foreground mt-1">
              Tools you&apos;ve chosen to always allow without approval prompts. Remove a rule to re-enable the confirmation dialog.
            </p>
          </div>
          <div className="flex-1 overflow-y-auto min-h-0">
            <ToolBypassRulesList />
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
