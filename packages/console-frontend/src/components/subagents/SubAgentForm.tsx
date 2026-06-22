import { useState, useEffect } from 'react';
import {
  Globe,
  Terminal,
  ChevronDown,
  Info,
  CheckCircle2,
  Lightbulb,
  Server,
  Code2,
  Database,
  Key,
  Users,
  ShieldAlert,
  Plus,
  Trash2,
  Pencil,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { useAvailableModels, modelSupportsThinking, getAvailableThinkingLevels, modelSelectOptions, MODEL_TIER_OPTIONS } from '@/config/models';
import type { SubAgent, SubAgentType, SubAgentFormData, SkillDefinition } from './types';
import type { OrchestratorThinkingLevel, SkillSearchResult } from '@/api/generated/types.gen';
import { SkillEditorModal } from '@/components/skills/SkillEditorModal';
import { SkillRegistryBrowseDialog } from '@/components/skills/SkillRegistryBrowseDialog';
import { MCPToolToggleList } from '@/components/settings/MCPToolToggleList';
import { ExtendedThinkingConfig } from '@/components/settings/ExtendedThinkingConfig';
import { PricingConfigurationSection } from '@/components/subagents/PricingConfigurationSection';
import { useQuery } from '@tanstack/react-query';
import { listSecretsApiV1SecretsGetOptions } from '@/api/generated/@tanstack/react-query.gen';
import { client } from '@/api/generated/client.gen';

interface SubAgentFormProps {
  subAgent?: SubAgent;
  onSubmit: (data: SubAgentFormData) => Promise<void>;
  onCancel: () => void;
  isSubmitting?: boolean;
}

export function SubAgentForm({ subAgent, onSubmit, onCancel, isSubmitting = false }: SubAgentFormProps) {
  const isEditing = !!subAgent;
  const { models: availableModels } = useAvailableModels();

  // Get config from embedded config_version
  const config = subAgent?.config_version;

  const [name, setName] = useState(subAgent?.name ?? '');
  const [description, setDescription] = useState(config?.description ?? '');
  // A local agent binds to EITHER a concrete model alias OR a capability tier. The Select
  // holds both in one value: a tier is encoded as `tier:<tier>`, anything else is an alias.
  const [modelSelection, setModelSelection] = useState<string>(
    config?.model_tier ? `tier:${config.model_tier}` : (config?.model ?? '')
  );
  const isTierSelected = modelSelection.startsWith('tier:');
  const modelAlias = isTierSelected ? '' : modelSelection;  // '' when a tier is chosen
  const modelTier = isTierSelected ? modelSelection.slice('tier:'.length) : null;
  const [type, setType] = useState<SubAgentType>(subAgent?.type ?? ('local' as SubAgentType));
  const [isPublic, setIsPublic] = useState(subAgent?.is_public ?? false);
  const [isMcpToolsOpen, setIsMcpToolsOpen] = useState(false);

  // Remote configuration
  const [agentUrl, setAgentUrl] = useState(config?.agent_url ?? '');

  // Local configuration
  const [systemPrompt, setSystemPrompt] = useState(config?.system_prompt ?? '');
  const [mcpTools, setMcpTools] = useState<string[]>(config?.mcp_tools ?? []);

  // Extended thinking configuration (local agents only)
  const [enableThinking, setEnableThinking] = useState(config?.enable_thinking ?? false);
  const [thinkingLevel, setThinkingLevel] = useState<OrchestratorThinkingLevel | null>(
    (config?.thinking_level as OrchestratorThinkingLevel) ?? null
  );

  // Foundry configuration
  const [foundryHostname, setFoundryHostname] = useState(config?.foundry_hostname ?? '');
  const [foundryClientId, setFoundryClientId] = useState(config?.foundry_client_id ?? '');
  const [foundryClientSecretRef, setFoundryClientSecretRef] = useState<number | null>(
    config?.foundry_client_secret_ref ?? null
  );
  const [foundryOntologyRid, setFoundryOntologyRid] = useState(config?.foundry_ontology_rid ?? '');
  const [foundryQueryApiName, setFoundryQueryApiName] = useState(config?.foundry_query_api_name ?? '');
  const [foundryScopes, setFoundryScopes] = useState<string[]>(config?.foundry_scopes ?? []);
  const [foundryVersion, setFoundryVersion] = useState(config?.foundry_version ?? '');

  // Pricing configuration (remote and foundry agents only)
  const pricingConfig = config?.pricing_config as any;
  const [rateCardEntries, setRateCardEntries] = useState<Array<{ billing_unit: string; price_per_million: string }>>(
    pricingConfig?.rate_card_entries?.map((e: any) => ({
      billing_unit: e.billing_unit,
      price_per_million: e.price_per_million.toString(),
    })) ?? [{ billing_unit: 'requests', price_per_million: '' }]
  );
  const [isPricingOpen, setIsPricingOpen] = useState(false);

  // Skills configuration (local agents only)
  const [skills, setSkills] = useState<Array<{ name: string; description: string; body: string; files?: Array<{ path: string; content: string }>; source?: string | null; source_hash?: string | null }>>(
    (config?.skills as SkillDefinition[] | undefined)?.map((s) => ({
      name: s.name ?? '',
      description: s.description ?? '',
      body: s.body ?? '',
      files: s.files?.map((f: { path: string; content: string }) => ({ path: f.path, content: f.content })),
      source: s.source ?? null,
      source_hash: s.content_hash ?? null,
    })) ?? []
  );
  const [isSkillModalOpen, setIsSkillModalOpen] = useState(false);
  const [isSkillImportOpen, setIsSkillImportOpen] = useState(false);
  const [importingSkillId, setImportingSkillId] = useState<string | null>(null);
  const [sandboxEnabled, setSandboxEnabled] = useState(config?.sandbox_enabled ?? false);
  const [sandboxAutoEnabled, setSandboxAutoEnabled] = useState(false);

  // Auto-enable sandbox when any skill has executable files
  const SANDBOX_EXTENSIONS = new Set(['.py', '.sh', '.bash', '.zsh', '.js', '.ts', '.rb', '.pl', '.ps1', '.bat', '.cmd', '.mjs', '.cjs']);
  useEffect(() => {
    const hasExecutableFiles = skills.some((skill) =>
      skill.files?.some((f) => {
        const ext = f.path.includes('.') ? '.' + f.path.split('.').pop()!.toLowerCase() : '';
        return SANDBOX_EXTENSIONS.has(ext);
      })
    );
    if (hasExecutableFiles && !sandboxEnabled) {
      setSandboxEnabled(true);
      setSandboxAutoEnabled(true);
    } else if (!hasExecutableFiles && sandboxAutoEnabled) {
      setSandboxEnabled(false);
      setSandboxAutoEnabled(false);
    }
  }, [skills]); // eslint-disable-line react-hooks/exhaustive-deps

  // Query for secrets
  const { data: secretsData, isLoading: isLoadingSecrets } = useQuery({
    ...listSecretsApiV1SecretsGetOptions(),
    enabled: type === 'foundry',
  });

  // Filter for foundry_client_secret type
  const availableSecrets = secretsData?.items?.filter((secret) => secret.secret_type === 'foundry_client_secret') ?? [];

  // Find the currently selected secret for display purposes
  const selectedSecret = availableSecrets.find((secret) => secret.id === foundryClientSecretRef);

  const handleImportSkillFromRegistry = async (skill: SkillSearchResult) => {
    if (!skill.id) return;
    setImportingSkillId(skill.id);
    try {
      const { data, error } = await client.get({
        url: '/api/v1/skills/registry/detail/{skill_id}',
        path: { skill_id: skill.id },
      });
      if (error || !data) {
        toast.error('Failed to fetch skill details');
        return;
      }
      const detail = data as { name?: string; description?: string; content_hash?: string; files?: Array<{ path: string; content: string }> };
      const skillMdFile = detail.files?.find((f) => f.path === 'SKILL.md');
      const body = skillMdFile?.content ?? '';
      const otherFiles = (detail.files ?? [])
        .filter((f) => f.path !== 'SKILL.md')
        .map((f) => ({ path: f.path, content: f.content }));
      const newSkill = {
        name: detail.name ?? skill.name,
        description: detail.description ?? '',
        body,
        files: otherFiles.length > 0 ? otherFiles : undefined,
        source: skill.id,
        source_hash: detail.content_hash ?? null,
      };
      if (skills.some((s) => s.name === newSkill.name)) {
        toast.error(`Skill "${newSkill.name}" is already added`);
        return;
      }
      setSkills((prev) => [...prev, newSkill]);
      toast.success(`Imported "${newSkill.name}"`);
      setIsSkillImportOpen(false);
    } finally {
      setImportingSkillId(null);
    }
  };

  // Automatically disable thinking when the chosen concrete model doesn't support it.
  // A tier selection has no concrete alias here (modelAlias === ''), so we skip these
  // capability checks — thinking is resolved against the tier's actual model at runtime.
  useEffect(() => {
    if (modelAlias && !modelSupportsThinking(modelAlias, availableModels)) {
      setEnableThinking(false);
    }
    // Reset thinking level to 'low' if current level is not available for this model
    if (modelAlias && enableThinking) {
      const availableLevels = getAvailableThinkingLevels(modelAlias, availableModels);
      const isCurrentLevelAvailable = availableLevels.some((opt) => opt.value === thinkingLevel);
      if (!isCurrentLevelAvailable) {
        setThinkingLevel('low'); // Default to 'low' which is supported by all models
      }
    }
  }, [modelAlias, enableThinking, thinkingLevel]);

  const validate = (): string | null => {
    if (!name.trim()) {
      return 'Name is required';
    }
    // Validate name format: only lowercase letters, numbers, and hyphens
    const namePattern = /^[a-z0-9-]+$/;
    if (!namePattern.test(name.trim())) {
      return 'Name must contain only lowercase letters, numbers, and hyphens';
    }
    if (!description.trim()) {
      return 'Description is required';
    }
    if (type === 'remote') {
      if (!agentUrl.trim()) {
        return 'Agent URL is required for remote agents';
      }
      try {
        new URL(agentUrl);
      } catch {
        return 'Agent URL must be a valid URL';
      }
    }
    if (type === 'local') {
      if (!modelSelection.trim()) {
        return 'Model or tier is required for local agents';
      }
      if (!systemPrompt.trim()) {
        return 'System prompt is required for local agents';
      }
    }
    if (type === 'foundry') {
      if (!foundryHostname.trim()) {
        return 'Foundry hostname is required';
      }
      if (!foundryClientId.trim()) {
        return 'Client ID is required';
      }
      if (!isEditing && !foundryClientSecretRef) {
        return 'Client Secret is required - please select a secret from the vault';
      }
      if (!foundryOntologyRid.trim()) {
        return 'Ontology RID is required';
      }
      if (!foundryQueryApiName.trim()) {
        return 'Query API Name is required';
      }
      if (foundryScopes.length === 0) {
        return 'At least one API scope is required';
      }
    }
    // Validate skills
    const skillNamePattern = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
    const seenNames = new Set<string>();
    for (const skill of skills) {
      if (!skill.name.trim()) {
        return 'Each skill must have a name';
      }
      if (!skillNamePattern.test(skill.name) || skill.name.includes('--')) {
        return `Skill name "${skill.name}" must be lowercase letters, numbers, and hyphens only (no leading/trailing/consecutive hyphens)`;
      }
      if (seenNames.has(skill.name)) {
        return `Duplicate skill name "${skill.name}"`;
      }
      seenNames.add(skill.name);
      if (!skill.description.trim()) {
        return `Skill "${skill.name}" must have a description`;
      }
    }
    return null;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const validationError = validate();
    if (validationError) {
      toast.error('Validation Error', { description: validationError });
      return;
    }

    let configuration;
    if (type === 'remote') {
      configuration = {
        agent_url: agentUrl.trim(),
        ...(rateCardEntries.length > 0 && rateCardEntries.some((e) => e.billing_unit && e.price_per_million)
          ? {
              pricing_config: {
                format: 'detailed',
                rate_card_entries: rateCardEntries
                  .filter((e) => e.billing_unit && e.price_per_million)
                  .map((e) => ({
                    billing_unit: e.billing_unit,
                    price_per_million: parseFloat(e.price_per_million),
                  })),
              },
            }
          : {}),
      };
    } else if (type === 'local') {
      configuration = {
        system_prompt: systemPrompt.trim(),
        ...(mcpTools.length > 0 && { mcp_tools: mcpTools }),
        enable_thinking: enableThinking,
        thinking_level: thinkingLevel ?? undefined, // Convert null to undefined for API
        ...(skills.length > 0 && { skills }),
        sandbox_enabled: sandboxEnabled,
      };
    } else if (type === 'foundry') {
      configuration = {
        foundry_hostname: foundryHostname.trim(),
        foundry_client_id: foundryClientId.trim(),
        foundry_client_secret_ref: foundryClientSecretRef,
        foundry_ontology_rid: foundryOntologyRid.trim(),
        foundry_query_api_name: foundryQueryApiName.trim(),
        foundry_scopes: foundryScopes,
        ...(foundryVersion.trim() && { foundry_version: foundryVersion.trim() }),
        ...(rateCardEntries.length > 0 && rateCardEntries.some((e) => e.billing_unit && e.price_per_million)
          ? {
              pricing_config: {
                format: 'detailed',
                rate_card_entries: rateCardEntries
                  .filter((e) => e.billing_unit && e.price_per_million)
                  .map((e) => ({
                    billing_unit: e.billing_unit,
                    price_per_million: parseFloat(e.price_per_million),
                  })),
              },
            }
          : {}),
      };
    }

    try {
      await onSubmit({
        name: name.trim(),
        description: description.trim(),
        model: type === 'local' && !isTierSelected ? modelAlias.trim() : undefined,
        model_tier: type === 'local' && isTierSelected ? (modelTier as SubAgentFormData['model_tier']) : undefined,
        type,
        is_public: isPublic,
        configuration: configuration!,
        ...(type === 'local' && skills.length > 0 && { skills }),
        ...(type === 'local' && { sandbox_enabled: sandboxEnabled }),
      });
    } catch (err) {
      toast.error('Error', { description: err instanceof Error ? err.message : 'An error occurred' });
    }
  };

  const handleTypeChange = (newType: SubAgentType) => {
    setType(newType);
    // Clear configuration fields when switching types
    if (newType === 'remote') {
      setSystemPrompt('');
      setMcpTools([]);
      setEnableThinking(false);
      setThinkingLevel('low');
      setFoundryHostname('');
      setFoundryClientId('');
      setFoundryClientSecretRef(null);
      setFoundryOntologyRid('');
      setFoundryQueryApiName('');
      setFoundryScopes([]);
      setFoundryVersion('');
      setRateCardEntries([{ billing_unit: 'requests', price_per_million: '' }]);
      setSkills([]);
      setSandboxEnabled(false);
    } else if (newType === 'local') {
      setAgentUrl('');
      setFoundryHostname('');
      setFoundryClientId('');
      setFoundryClientSecretRef(null);
      setFoundryOntologyRid('');
      setFoundryQueryApiName('');
      setFoundryScopes([]);
      setFoundryVersion('');
      setRateCardEntries([{ billing_unit: 'requests', price_per_million: '' }]);
    } else if (newType === 'foundry') {
      setAgentUrl('');
      setSystemPrompt('');
      setMcpTools([]);
      setEnableThinking(false);
      setThinkingLevel('low');
      setSkills([]);
      setSandboxEnabled(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      {/* Type Selection Cards - Prominent at the top */}
      {!isEditing && (
        <div>
          <h3 className="text-lg font-semibold mb-4">Choose Agent Type</h3>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* Local Agent Card */}
            <button
              type="button"
              onClick={() => handleTypeChange('local')}
              disabled={isSubmitting}
              className={cn(
                'relative flex flex-col items-start p-6 rounded-lg border-2 transition-all text-left',
                'hover:shadow-md focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
                type === 'local'
                  ? 'border-primary bg-primary/5 shadow-sm'
                  : 'border-border bg-background hover:border-primary/50'
              )}
            >
              {type === 'local' && (
                <div className="absolute top-3 right-3">
                  <CheckCircle2 className="h-5 w-5 text-primary" />
                </div>
              )}
              <div className="flex items-center gap-3 mb-3">
                <div className={cn('p-2 rounded-md', type === 'local' ? 'bg-primary/10' : 'bg-muted')}>
                  <Terminal className="h-6 w-6" />
                </div>
                <h4 className="text-base font-semibold">Local Agent</h4>
              </div>
              <p className="text-sm text-muted-foreground">
                Run an agent locally with a custom system prompt and optional MCP tools. Full control over behavior and
                capabilities.
              </p>
            </button>

            {/* Remote Agent Card */}
            <button
              type="button"
              onClick={() => handleTypeChange('remote')}
              disabled={isSubmitting}
              className={cn(
                'relative flex flex-col items-start p-6 rounded-lg border-2 transition-all text-left',
                'hover:shadow-md focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
                type === 'remote'
                  ? 'border-primary bg-primary/5 shadow-sm'
                  : 'border-border bg-background hover:border-primary/50'
              )}
            >
              {type === 'remote' && (
                <div className="absolute top-3 right-3">
                  <CheckCircle2 className="h-5 w-5 text-primary" />
                </div>
              )}
              <div className="flex items-center gap-3 mb-3">
                <div className={cn('p-2 rounded-md', type === 'remote' ? 'bg-primary/10' : 'bg-muted')}>
                  <Globe className="h-6 w-6" />
                </div>
                <h4 className="text-base font-semibold">Remote Agent (A2A)</h4>
              </div>
              <p className="text-sm text-muted-foreground">
                Connect to an external A2A-compatible agent endpoint. Delegate tasks to specialized external services.
              </p>
            </button>

            {/* Foundry Agent Card */}
            <button
              type="button"
              onClick={() => handleTypeChange('foundry' as SubAgentType)}
              disabled={isSubmitting}
              className={cn(
                'relative flex flex-col items-start p-6 rounded-lg border-2 transition-all text-left',
                'hover:shadow-md focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
                type === 'foundry'
                  ? 'border-primary bg-primary/5 shadow-sm'
                  : 'border-border bg-background hover:border-primary/50'
              )}
            >
              {type === 'foundry' && (
                <div className="absolute top-3 right-3">
                  <CheckCircle2 className="h-5 w-5 text-primary" />
                </div>
              )}
              <div className="flex items-center gap-3 mb-3">
                <div className={cn('p-2 rounded-md', type === 'foundry' ? 'bg-primary/10' : 'bg-muted')}>
                  <Database className="h-6 w-6" />
                </div>
                <h4 className="text-base font-semibold">Foundry Agent</h4>
              </div>
              <p className="text-sm text-muted-foreground">
                Connect to Palantir Foundry ontology queries. Execute data operations and workflows on Foundry.
              </p>
            </button>
          </div>
        </div>
      )}

      {/* Two-Column Layout for Desktop */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Form Column */}
        <div className="lg:col-span-2 space-y-6">
          {/* Basic Information Card */}
          <Card>
            <CardHeader>
              <CardTitle>Basic Information</CardTitle>
              <CardDescription>Provide a name and description for your sub-agent</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="name">Name *</Label>
                <Input
                  id="name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="my-sub-agent"
                  disabled={isSubmitting}
                />
                <p className="text-xs text-muted-foreground">Only lowercase letters, numbers, and hyphens allowed</p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="description">Description *</Label>
                <Textarea
                  id="description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Describe the agent's skills and capabilities."
                  rows={3}
                  disabled={isSubmitting}
                />
                <p className="text-xs text-muted-foreground">What tasks can this agent handle?</p>
              </div>

              <div className="flex items-center justify-between space-x-2">
                <div className="space-y-0.5">
                  <Label htmlFor="is_public" className="flex items-center gap-2">
                    <Users className="h-4 w-4" />
                    Public Access
                  </Label>
                  <p className="text-xs text-muted-foreground">
                    When enabled, all users can access this sub-agent without group permissions
                  </p>
                </div>
                <Switch id="is_public" checked={isPublic} onCheckedChange={setIsPublic} disabled={isSubmitting} />
              </div>

              {/* Show type if editing (can't change) */}
              {isEditing && (
                <div className="space-y-2">
                  <Label>Type</Label>
                  <div className="flex items-center gap-2 p-3 rounded-md bg-muted">
                    {type === 'local' ? (
                      <>
                        <Terminal className="h-4 w-4" />
                        <span className="font-medium">Local Agent</span>
                      </>
                    ) : type === 'remote' ? (
                      <>
                        <Globe className="h-4 w-4" />
                        <span className="font-medium">Remote Agent (A2A)</span>
                      </>
                    ) : (
                      <>
                        <Database className="h-4 w-4" />
                        <span className="font-medium">Foundry Agent</span>
                      </>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">Agent type cannot be changed after creation</p>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Configuration Card */}
          <Card>
            <CardHeader>
              <CardTitle>Configuration</CardTitle>
              <CardDescription>
                {type === 'local'
                  ? 'Configure the model and behavior for your local agent'
                  : type === 'remote'
                    ? 'Configure the connection to your remote agent'
                    : 'Configure the Foundry connection and query details'}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {type === 'remote' ? (
                <div className="space-y-2">
                  <Label htmlFor="agentUrl">Agent URL *</Label>
                  <Input
                    id="agentUrl"
                    value={agentUrl}
                    onChange={(e) => setAgentUrl(e.target.value)}
                    placeholder="https://my-agent.example.com/a2a"
                    disabled={isSubmitting}
                  />
                  <p className="text-xs text-muted-foreground flex items-start gap-1.5">
                    <Server className="h-3 w-3 mt-0.5 flex-shrink-0" />
                    <span>The A2A endpoint URL of the remote agent</span>
                  </p>
                </div>
              ) : type === 'foundry' ? (
                <>
                  <div className="space-y-2">
                    <Label htmlFor="foundryHostname">Foundry Hostname *</Label>
                    <Input
                      id="foundryHostname"
                      value={foundryHostname}
                      onChange={(e) => setFoundryHostname(e.target.value)}
                      placeholder="example.palantirfoundry.com"
                      disabled={isSubmitting}
                    />
                    <p className="text-xs text-muted-foreground">Your Foundry instance hostname (without https://)</p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="foundryClientId">Client ID *</Label>
                    <Input
                      id="foundryClientId"
                      value={foundryClientId}
                      onChange={(e) => setFoundryClientId(e.target.value)}
                      placeholder="client-id-from-foundry"
                      disabled={isSubmitting}
                    />
                    <p className="text-xs text-muted-foreground">OAuth2 Client ID from Foundry</p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="foundryClientSecretRef" className="flex items-center gap-2">
                      <Key className="h-4 w-4" />
                      Client Secret {!isEditing && '*'}
                    </Label>
                    <Select
                      value={foundryClientSecretRef?.toString() ?? ''}
                      onValueChange={(value) => setFoundryClientSecretRef(value ? parseInt(value) : null)}
                      disabled={isSubmitting || isLoadingSecrets}
                    >
                      <SelectTrigger id="foundryClientSecretRef">
                        <SelectValue>
                          {foundryClientSecretRef && selectedSecret ? (
                            <>
                              {selectedSecret.name}
                              {selectedSecret.description && (
                                <span className="text-xs text-muted-foreground ml-2">
                                  - {selectedSecret.description}
                                </span>
                              )}
                            </>
                          ) : foundryClientSecretRef && !selectedSecret ? (
                            <span className="text-muted-foreground">
                              Secret ID: {foundryClientSecretRef} (not found in vault)
                            </span>
                          ) : isLoadingSecrets ? (
                            <span className="text-muted-foreground">Loading secrets...</span>
                          ) : (
                            <span className="text-muted-foreground">Select a secret from vault</span>
                          )}
                        </SelectValue>
                      </SelectTrigger>
                      <SelectContent>
                        {isLoadingSecrets ? (
                          <div className="p-4 text-center text-sm text-muted-foreground">Loading secrets...</div>
                        ) : availableSecrets.length === 0 ? (
                          <div className="p-4 text-center text-sm text-muted-foreground">
                            No secrets available. Create a Foundry Client Secret in Settings → Secrets Vault first.
                          </div>
                        ) : (
                          availableSecrets.map((secret) => (
                            <SelectItem key={secret.id} value={secret.id.toString()}>
                              {secret.name}
                              {secret.description && (
                                <span className="text-xs text-muted-foreground ml-2">- {secret.description}</span>
                              )}
                            </SelectItem>
                          ))
                        )}
                      </SelectContent>
                    </Select>
                    <p className="text-xs text-muted-foreground">
                      {isEditing
                        ? 'Select a secret from the vault or keep the existing one.'
                        : 'Select a secret from the vault. Secrets are stored securely in AWS SSM Parameter Store.'}
                    </p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="foundryOntologyRid">Ontology RID *</Label>
                    <Input
                      id="foundryOntologyRid"
                      value={foundryOntologyRid}
                      onChange={(e) => setFoundryOntologyRid(e.target.value)}
                      placeholder="ri.ontology.main.ontology.xxx"
                      disabled={isSubmitting}
                    />
                    <p className="text-xs text-muted-foreground">The Resource Identifier for your Foundry ontology</p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="foundryQueryApiName">Query API Name *</Label>
                    <Input
                      id="foundryQueryApiName"
                      value={foundryQueryApiName}
                      onChange={(e) => setFoundryQueryApiName(e.target.value)}
                      placeholder="myQueryApi"
                      disabled={isSubmitting}
                    />
                    <p className="text-xs text-muted-foreground">The name of the query API to execute</p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="foundryScopes">API Scopes *</Label>
                    <div className="grid grid-cols-1 gap-2">
                      {[
                        { value: 'api:use-ontologies-read', label: 'Ontologies Read' },
                        { value: 'api:use-ontologies-write', label: 'Ontologies Write' },
                        { value: 'api:use-aip-agents-read', label: 'AIP Agents Read' },
                        { value: 'api:use-aip-agents-write', label: 'AIP Agents Write' },
                        { value: 'api:use-mediasets-read', label: 'Mediasets Read' },
                        { value: 'api:use-mediasets-write', label: 'Mediasets Write' },
                      ].map((scope) => (
                        <label key={scope.value} className="flex items-center gap-2 text-sm">
                          <input
                            type="checkbox"
                            checked={foundryScopes.includes(scope.value)}
                            onChange={(e) => {
                              if (e.target.checked) {
                                setFoundryScopes([...foundryScopes, scope.value]);
                              } else {
                                setFoundryScopes(foundryScopes.filter((s) => s !== scope.value));
                              }
                            }}
                            disabled={isSubmitting}
                            className="rounded border-gray-300"
                          />
                          <span>{scope.label}</span>
                        </label>
                      ))}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Select the OAuth2 scopes required for your Foundry operations
                    </p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="foundryVersion">Foundry Version (Optional)</Label>
                    <Input
                      id="foundryVersion"
                      value={foundryVersion}
                      onChange={(e) => setFoundryVersion(e.target.value)}
                      placeholder="v1"
                      disabled={isSubmitting}
                    />
                    <p className="text-xs text-muted-foreground">The version of the Foundry query API (e.g., v1, v2)</p>
                  </div>
                </>
              ) : (
                <>
                  <div className="space-y-2">
                    <Label htmlFor="model">Model *</Label>
                    <Select value={modelSelection} onValueChange={setModelSelection} disabled={isSubmitting}>
                      <SelectTrigger id="model">
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
                          {modelSelectOptions(modelAlias, availableModels, config?.model_retired ?? false).options.map((option) => (
                            <SelectItem key={option.value} value={option.value}>
                              {option.label}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                    {modelSelectOptions(modelAlias, availableModels, config?.model_retired ?? false).retiredValue ? (
                      <p className="text-xs text-amber-600 dark:text-amber-400">
                        This model was retired. Select a replacement to update the agent.
                      </p>
                    ) : isTierSelected ? (
                      <p className="text-xs text-muted-foreground">
                        Runs on whichever model is the current default for this tier — survives model upgrades.
                      </p>
                    ) : (
                      <p className="text-xs text-muted-foreground">The LLM model to use for this agent</p>
                    )}
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="systemPrompt">System Prompt *</Label>
                    <Textarea
                      id="systemPrompt"
                      value={systemPrompt}
                      onChange={(e) => setSystemPrompt(e.target.value)}
                      placeholder="You are a helpful assistant that..."
                      rows={6}
                      disabled={isSubmitting}
                      className="font-mono text-sm"
                    />
                    <p className="text-xs text-muted-foreground flex items-start gap-1.5">
                      <Code2 className="h-3 w-3 mt-0.5 flex-shrink-0" />
                      <span>The system prompt that defines the agent's behavior</span>
                    </p>
                  </div>

                  {/* Extended Thinking Configuration */}
                  <ExtendedThinkingConfig
                    model={modelAlias}
                    enableThinking={enableThinking}
                    thinkingLevel={thinkingLevel}
                    onEnableThinkingChange={(checked) => {
                      setEnableThinking(checked);
                      if (!checked) {
                        setThinkingLevel(null);
                      } else if (thinkingLevel === null) {
                        setThinkingLevel('low');
                      }
                    }}
                    onThinkingLevelChange={setThinkingLevel}
                    disabled={isSubmitting}
                    showAsCard={false}
                  />
                </>
              )}
            </CardContent>
          </Card>

          {/* Pricing Configuration Card - Collapsible, only for remote and foundry agents */}
          {(type === 'remote' || type === 'foundry') && (
            <PricingConfigurationSection
              isEditing={true}
              expanded={isPricingOpen}
              onExpandedChange={setIsPricingOpen}
              rateCardEntries={rateCardEntries}
              onRateCardEntriesChange={setRateCardEntries}
              disabled={isSubmitting}
              asCard={true}
            />
          )}

          {/* MCP Tools Card - Collapsible, only for local agents */}
          {type === 'local' && (
            <Card>
              <Collapsible open={isMcpToolsOpen} onOpenChange={setIsMcpToolsOpen}>
                <CardHeader>
                  <CollapsibleTrigger className="flex w-full items-center justify-between hover:opacity-80 transition-opacity [&[data-state=open]>svg]:rotate-180">
                    <div className="text-left">
                      <CardTitle className="flex items-center gap-2">
                        MCP Tools (Optional)
                        {mcpTools.length > 0 && (
                          <span className="text-sm font-normal text-muted-foreground">
                            ({mcpTools.length} selected)
                          </span>
                        )}
                      </CardTitle>
                      <CardDescription>
                        {isMcpToolsOpen
                          ? "Select tools to extend your agent's capabilities"
                          : `${mcpTools.length === 0 ? 'No tools selected' : `${mcpTools.length} tools selected`} - Click to ${isMcpToolsOpen ? 'collapse' : 'expand'}`}
                      </CardDescription>
                    </div>
                    <ChevronDown className="h-5 w-5 text-muted-foreground flex-shrink-0 transition-transform duration-200" />
                  </CollapsibleTrigger>
                </CardHeader>
                <CollapsibleContent>
                  <CardContent>
                    <div className="max-h-[600px] overflow-y-auto">
                      <MCPToolToggleList value={mcpTools} onChange={setMcpTools} disabled={isSubmitting} />
                    </div>
                    <p className="text-xs text-muted-foreground mt-4">
                      If no tools are selected, the agent will inherit the orchestrator's tools.
                    </p>
                  </CardContent>
                </CollapsibleContent>
              </Collapsible>
            </Card>
          )}

          {/* Skills Card - Compact summary + modal editor, only for local agents */}
          {type === 'local' && (
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div className="text-left">
                    <CardTitle className="flex items-center gap-2">
                      Skills (Optional)
                      {skills.length > 0 && (
                        <span className="text-sm font-normal text-muted-foreground">
                          ({skills.length} defined)
                        </span>
                      )}
                    </CardTitle>
                    <CardDescription>
                      {skills.length === 0
                        ? 'No skills defined — import from registry or create custom skills'
                        : 'Reusable workflow skills for this agent'}
                    </CardDescription>
                  </div>
                  <div className="flex gap-1.5">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setIsSkillImportOpen(true)}
                      disabled={isSubmitting}
                    >
                      <Plus className="h-4 w-4 mr-1" />
                      Import
                    </Button>
                    {skills.some((s) => !(s as any).source) ? (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => setIsSkillModalOpen(true)}
                        disabled={isSubmitting}
                      >
                        <Pencil className="h-4 w-4 mr-1" />
                        Edit Custom
                      </Button>
                    ) : (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => setIsSkillModalOpen(true)}
                        disabled={isSubmitting}
                      >
                        <Plus className="h-4 w-4 mr-1" />
                        Create
                      </Button>
                    )}
                  </div>
                </div>
              </CardHeader>
              {skills.length > 0 && (
                <CardContent className="pt-0">
                  <div className="space-y-1">
                    {skills.map((skill, idx) => (
                      <div
                        key={idx}
                        className="flex items-center gap-2 py-1.5 px-2 rounded bg-muted/40 text-sm group/skill"
                      >
                        <code className="font-mono text-xs font-medium shrink-0">{skill.name || '(unnamed)'}</code>
                        {(skill as any).source && (
                          <span className="text-[10px] text-muted-foreground bg-muted px-1 rounded shrink-0">imported</span>
                        )}
                        {(skill.files?.length ?? 0) > 0 && (
                          <span className="text-[10px] text-muted-foreground shrink-0">
                            {skill.files!.length} files
                          </span>
                        )}
                        {skill.description && (
                          <span className="text-xs text-muted-foreground truncate flex-1">
                            — {skill.description.length > 50 ? skill.description.slice(0, 50) + '…' : skill.description}
                          </span>
                        )}
                        <button
                          type="button"
                          className="opacity-0 group-hover/skill:opacity-100 text-destructive hover:text-destructive/80 transition-opacity ml-auto shrink-0"
                          onClick={() => setSkills((prev) => prev.filter((_, i) => i !== idx))}
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </div>
                    ))}
                  </div>
                </CardContent>
              )}
            </Card>
          )}

          {/* Skill import from registry dialog */}
          <SkillRegistryBrowseDialog
            open={isSkillImportOpen}
            onOpenChange={setIsSkillImportOpen}
            title="Add skill from registry"
            description="Search for a skill to import into this agent's configuration."
            actionLabel="Import"
            onAction={(skill) => handleImportSkillFromRegistry(skill)}
            actionPending={!!importingSkillId}
          />

          <SkillEditorModal
            open={isSkillModalOpen}
            onOpenChange={setIsSkillModalOpen}
            skills={skills.filter((s) => !(s as any).source)}
            onChange={(updated) => {
              const importedSkills = skills.filter((s) => (s as any).source);
              setSkills([...importedSkills, ...updated.map(s => ({ ...s, name: s.name ?? '', description: s.description ?? '', body: s.body ?? '' }))]);
            }}
            disabled={isSubmitting}
          />

          {/* Sandbox Toggle - only for local agents */}
          {type === 'local' && (
            <Card>
              <CardContent className="pt-6">
                <div className="flex items-center justify-between space-x-2">
                  <div className="space-y-0.5">
                    <Label htmlFor="sandbox_enabled" className="flex items-center gap-2">
                      <ShieldAlert className="h-4 w-4" />
                      Sandbox Execution
                    </Label>
                    <p className="text-xs text-muted-foreground">
                      When enabled, skill scripts will execute in an isolated sandbox environment.
                      Requires a sandbox provider to be configured on the server.
                    </p>
                  </div>
                  <Switch
                    id="sandbox_enabled"
                    checked={sandboxEnabled}
                    onCheckedChange={(checked) => {
                      setSandboxEnabled(checked);
                      if (!checked) setSandboxAutoEnabled(false);
                    }}
                    disabled={isSubmitting}
                  />
                </div>
                {sandboxAutoEnabled && (
                  <p className="text-xs text-amber-600 dark:text-amber-400 mt-2">
                    Auto-enabled because one or more skills contain executable files (.py, .sh, etc.)
                  </p>
                )}
              </CardContent>
            </Card>
          )}
        </div>

        {/* Help Sidebar */}
        <div className="lg:col-span-1">
          <div className="sticky top-6 space-y-4">
            <Card className="bg-muted/50">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Lightbulb className="h-4 w-4" />
                  Tips
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {type === 'local' ? (
                  <>
                    <div className="text-sm">
                      <p className="font-medium mb-1">Description Best Practices</p>
                      <p className="text-muted-foreground text-xs">
                        Be specific about what this agent can do. The orchestrator uses the description to decide when
                        to delegate tasks to this agent.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">System Prompt</p>
                      <p className="text-muted-foreground text-xs">
                        Define the agent's behavior, personality, and expertise. This guides how the agent responds to
                        tasks.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">MCP Tools</p>
                      <p className="text-muted-foreground text-xs">
                        Select tools that match your agent's purpose. For example, a code review agent might need git
                        and file system tools.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">Model Selection</p>
                      <p className="text-muted-foreground text-xs">
                        GPT-4o: balanced performance. GPT-4o Mini: cost-effective for simpler tasks. Claude Sonnet 4.5:
                        best for complex reasoning. Claude Sonnet 4.6: improved reasoning over 4.5. Claude Haiku 4.5:
                        ultra-fast and efficient.
                      </p>
                    </div>
                  </>
                ) : type === 'remote' ? (
                  <>
                    <div className="text-sm">
                      <p className="font-medium mb-1">Description Best Practices</p>
                      <p className="text-muted-foreground text-xs">
                        Be specific about what this agent can do. The orchestrator uses the description to decide when
                        to delegate tasks to this agent.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">A2A Protocol</p>
                      <p className="text-muted-foreground text-xs">
                        Remote agents must implement the A2A (Agent-to-Agent) protocol for communication.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">URL Format</p>
                      <p className="text-muted-foreground text-xs">
                        Provide the full endpoint URL including the protocol (https://). The endpoint should be
                        accessible from this environment.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">Authentication</p>
                      <p className="text-muted-foreground text-xs">
                        Ensure your remote agent is configured to accept requests from this orchestrator instance.
                      </p>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="text-sm">
                      <p className="font-medium mb-1">Description Best Practices</p>
                      <p className="text-muted-foreground text-xs">
                        Be specific about what this agent can do. The orchestrator uses the description to decide when
                        to delegate tasks to this agent.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">Secure Credentials</p>
                      <p className="text-muted-foreground text-xs">
                        Client secrets are securely stored in AWS SSM Parameter Store and encrypted with KMS. They are
                        never stored in the database.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">Query API</p>
                      <p className="text-muted-foreground text-xs">
                        The Query API Name should match an existing query API defined in your Foundry ontology.
                      </p>
                    </div>
                    <div className="text-sm">
                      <p className="font-medium mb-1">Scopes</p>
                      <p className="text-muted-foreground text-xs">
                        Select only the minimum scopes required for your agent's operations to follow the principle of
                        least privilege.
                      </p>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>

            <Card className="bg-muted/50">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Info className="h-4 w-4" />
                  Example
                </CardTitle>
              </CardHeader>
              <CardContent>
                {type === 'local' ? (
                  <div className="text-sm space-y-2">
                    <p className="font-medium">Code Review Agent</p>
                    <div className="text-xs text-muted-foreground space-y-1">
                      <p>
                        <strong>Description:</strong> "Reviews code for best practices, security issues, and performance
                        optimizations"
                      </p>
                      <p>
                        <strong>Model:</strong> Claude Sonnet 4.5
                      </p>
                      <p>
                        <strong>Tools:</strong> git, filesystem
                      </p>
                    </div>
                  </div>
                ) : type === 'remote' ? (
                  <div className="text-sm space-y-2">
                    <p className="font-medium">JIRA Integration</p>
                    <div className="text-xs text-muted-foreground space-y-1">
                      <p>
                        <strong>Description:</strong> "Manages JIRA tickets and project tracking"
                      </p>
                      <p>
                        <strong>URL:</strong> https://jira-agent.example.com/a2a
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="text-sm space-y-2">
                    <p className="font-medium">Ticket Creation Agent</p>
                    <div className="text-xs text-muted-foreground space-y-1">
                      <p>
                        <strong>Description:</strong> "Creates tickets in Foundry based on user requests"
                      </p>
                      <p>
                        <strong>Ontology:</strong> ri.ontology.main.ontology.xxx
                      </p>
                      <p>
                        <strong>Query API:</strong> createTicketQuery
                      </p>
                      <p>
                        <strong>Scopes:</strong> Ontologies Write
                      </p>
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      </div>

      {/* Actions */}
      <div className="flex justify-end gap-3 pt-4 border-t sticky bottom-0 bg-background py-4">
        <Button type="button" variant="outline" onClick={onCancel} disabled={isSubmitting}>
          Cancel
        </Button>
        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? 'Saving...' : isEditing ? 'Save Changes' : 'Create Sub-Agent'}
        </Button>
      </div>
    </form>
  );
}
