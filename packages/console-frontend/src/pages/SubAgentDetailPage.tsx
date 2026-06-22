import { useState, useEffect, useRef, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft,
  Edit,
  Save,
  X,
  Globe,
  Terminal,
  FlaskConical,
  Send,
  AlertCircle,
  Loader2,
  Plus,
  MessageSquare,
  Trash2,
  Clock,
  CheckCircle,
  XCircle,
  Users,
  Info,
  PanelRightOpen,
  HelpCircle,
  Maximize2,
  Eye,
  Code,
  Wrench,
  ChevronDown,
  Lock,
  Unlock,
  Database,
  Key,
  AlertTriangle,
  Pencil,
  Cpu,
  FileText,
  Plug,
  ExternalLink,
  Blocks,
  ArrowUpCircle,
  RefreshCw,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Switch } from '@/components/ui/switch';
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { ApprovalDialog } from '@/components/subagents/ApprovalDialog';
import { SubAgentPermissionsDialog } from '@/components/subagents/SubAgentPermissionsDialog';
import { VersionSidebar } from '@/components/subagents/VersionSidebar';
import { MCPToolToggleList } from '@/components/settings/MCPToolToggleList';
import { PricingConfigurationSection } from '@/components/subagents/PricingConfigurationSection';
import { ConfigSection } from '@/components/subagents/ConfigSection';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import { getErrorMessage } from '@/lib/utils';
import { useAvailableModels, modelSupportsThinking, getAvailableThinkingLevels, modelSelectOptions, MODEL_TIER_OPTIONS } from '@/config/models';
import { ModelStatusText } from '@/components/models/ModelStatusText';
import {
  getSubAgentApiV1SubAgentsSubAgentIdGetOptions,
  getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGetOptions,
  consoleUpdateSubAgentMutation,
  deleteSubAgentApiV1SubAgentsSubAgentIdDeleteMutation,
  submitForApprovalApiV1SubAgentsSubAgentIdSubmitPostMutation,
  reviewVersionApiV1SubAgentsSubAgentIdVersionsVersionReviewPostMutation,
  listSecretsApiV1SecretsGetOptions,
  getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetOptions,
  listActivationsApiV1SkillsActivationsSubAgentIdGetOptions,
  listActivationsApiV1SkillsActivationsSubAgentIdGetQueryKey,
  activateSkillApiV1SkillsActivationsPostMutation,
  deactivateSkillApiV1SkillsActivationsActivationIdDeleteMutation,
  updateActivationApiV1SkillsActivationsActivationIdUpdatePostMutation,
  listMyGroupsApiV1GroupsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { SubAgentConfigVersion, OrchestratorThinkingLevel, SkillDefinition, McpSkillFile, SkillSearchResult, SkillActivationWithStatus, ModelTier } from '@/api/generated/types.gen';
import type { SubAgentStatus } from '@/components/subagents/types';
import { client } from '@/api/generated/client.gen';
import { Markdown } from '@/components/ui/markdown';
import { usePlaygroundChat } from '@/hooks/usePlaygroundChat';
import { SkillEditorModal } from '@/components/skills/SkillEditorModal';
import { SkillRegistryBrowseDialog } from '@/components/skills/SkillRegistryBrowseDialog';
import { SkillDiffDialog } from '@/components/skills/SkillDiffDialog';

const statusConfig: Record<
  SubAgentStatus,
  { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline'; icon: typeof Clock }
> = {
  draft: { label: 'Draft', variant: 'secondary', icon: Edit },
  pending_approval: { label: 'Pending Approval', variant: 'outline', icon: Clock },
  approved: { label: 'Approved', variant: 'default', icon: CheckCircle },
  rejected: { label: 'Rejected', variant: 'destructive', icon: XCircle },
};

type SkillDiffInfo = {
  registryId: string;
  contentHash: string;
  name: string;
  updateTarget?: { type: 'imported-skill'; skillName: string } | { type: 'imported-skill-direct'; skillName: string } | { type: 'activation'; activationId: number };
};

export function SubAgentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user, adminMode, isImpersonating } = useAuth();
  const { models: availableModels } = useAvailableModels();
  const [isEditing, setIsEditing] = useState(false);
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showPermissionsDialog, setShowPermissionsDialog] = useState(false);
  const [approvalAction, setApprovalAction] = useState<'approve' | 'reject' | null>(null);

  // Submit for approval dialog state
  const [showSubmitDialog, setShowSubmitDialog] = useState(false);
  const [submitChangeSummary, setSubmitChangeSummary] = useState('');

  // Version viewing state - null means viewing current version
  const [viewingVersionNumber, setViewingVersionNumber] = useState<number | null>(null);

  // Panel width configuration (persisted in localStorage)
  const [configPanelWidth, setConfigPanelWidth] = useState<'compact' | 'medium' | 'wide'>(() => {
    const saved = localStorage.getItem('subagent-config-panel-width');
    return (saved as 'compact' | 'medium' | 'wide') || 'medium';
  });

  // Smart panel layout state
  const [activeFocusArea, setActiveFocusArea] = useState<'config' | 'chat' | 'version' | null>(null);
  const [layoutLocked, setLayoutLocked] = useState(() => {
    const saved = localStorage.getItem('subagent-layout-locked');
    return saved === 'true';
  });
  const [screenWidth, setScreenWidth] = useState(() => window.innerWidth);
  const focusTimeoutRef = useRef<number | null>(null);

  // MCP Tools Sheet state
  const [showMcpToolsSheet, setShowMcpToolsSheet] = useState(false);

  // System prompt tab state (edit/preview)
  const [systemPromptTab, setSystemPromptTab] = useState<'edit' | 'preview'>('edit');

  // Change summary dialog state
  const [showChangeSummaryDialog, setShowChangeSummaryDialog] = useState(false);
  const [changeSummary, setChangeSummary] = useState('');

  // MCP tools expanded state for view mode
  const [mcpToolsExpanded, setMcpToolsExpanded] = useState(false);

  // Editable configuration state
  const [editName, setEditName] = useState('');
  const [editIsPublic, setEditIsPublic] = useState(false);
  const [editDescription, setEditDescription] = useState('');
  const [editModel, setEditModel] = useState('');
  const [editAgentUrl, setEditAgentUrl] = useState('');
  const [editSystemPrompt, setEditSystemPrompt] = useState('');
  const [editMcpTools, setEditMcpTools] = useState<string[]>([]);
  const [editEnableThinking, setEditEnableThinking] = useState(false);
  const [editThinkingLevel, setEditThinkingLevel] = useState<OrchestratorThinkingLevel | null>(null);

  // Foundry configuration state
  const [editFoundryHostname, setEditFoundryHostname] = useState('');
  const [editFoundryClientId, setEditFoundryClientId] = useState('');
  const [editFoundryClientSecretRef, setEditFoundryClientSecretRef] = useState<number | null>(null);
  const [editFoundryOntologyRid, setEditFoundryOntologyRid] = useState('');
  const [editFoundryQueryApiName, setEditFoundryQueryApiName] = useState('');
  const [editFoundryScopes, setEditFoundryScopes] = useState<string[]>([]);
  const [editFoundryVersion, setEditFoundryVersion] = useState('');

  // Pricing configuration state (remote and foundry agents only)
  const [editRateCardEntries, setEditRateCardEntries] = useState<
    Array<{ billing_unit: string; price_per_million: string }>
  >([{ billing_unit: 'requests', price_per_million: '' }]);
  const [pricingExpanded, setPricingExpanded] = useState(false);

  // Skills and sandbox state (local agents only)
  const [editSkills, setEditSkills] = useState<SkillDefinition[]>([]);
  const [editSandboxEnabled, setEditSandboxEnabled] = useState(false);
  const [editSandboxAutoEnabled, setEditSandboxAutoEnabled] = useState(false);
  const [isSkillModalOpen, setIsSkillModalOpen] = useState(false);
  const [isSkillImportOpen, setIsSkillImportOpen] = useState(false);
  const [importingSkillId, setImportingSkillId] = useState<string | null>(null);
  const [updatingSkillName, setUpdatingSkillName] = useState<string | null>(null);
  const [skillsWithUpdates, setSkillsWithUpdates] = useState<Set<string>>(new Set());

  // Skill diff dialog state
  const [skillDiffInfo, setSkillDiffInfo] = useState<SkillDiffInfo | null>(null);

  // Personal/group skill activations state
  const [showActivateSkillDialog, setShowActivateSkillDialog] = useState(false);
  const [deactivatingActivation, setDeactivatingActivation] = useState<SkillActivationWithStatus | null>(null);
  const [activateScope, setActivateScope] = useState<'personal' | 'group'>('personal');
  const [activateGroupId, setActivateGroupId] = useState<number | null>(null);

  // Chat input state
  const [inputValue, setInputValue] = useState('');
  const [showConversationList, setShowConversationList] = useState(false);
  const [versionSidebarCollapsed, setVersionSidebarCollapsed] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Auto-enable sandbox when any skill has executable files
  const SANDBOX_EXTENSIONS = useMemo(() => new Set(['.py', '.sh', '.bash', '.zsh', '.js', '.ts', '.rb', '.pl', '.ps1', '.bat', '.cmd', '.mjs', '.cjs']), []);
  useEffect(() => {
    const hasExecutableFiles = editSkills.some((skill) =>
      skill.files?.some((f) => {
        const ext = f.path.includes('.') ? '.' + f.path.split('.').pop()!.toLowerCase() : '';
        return SANDBOX_EXTENSIONS.has(ext);
      })
    );
    if (hasExecutableFiles && !editSandboxEnabled) {
      setEditSandboxEnabled(true);
      setEditSandboxAutoEnabled(true);
    } else if (!hasExecutableFiles && editSandboxAutoEnabled) {
      setEditSandboxEnabled(false);
      setEditSandboxAutoEnabled(false);
    }
  }, [editSkills]); // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch sub-agent
  const { data: subAgent } = useQuery({
    ...getSubAgentApiV1SubAgentsSubAgentIdGetOptions({
      path: { sub_agent_id: parseInt(id || '0', 10) },
    }),
    enabled: !!id,
  });

  // Determine if this is a local agent type
  const isFoundryAgentType = subAgent?.type === 'foundry';

  // Fetch available secrets for Foundry configuration
  const { data: secretsData } = useQuery({
    ...listSecretsApiV1SecretsGetOptions(),
    enabled: isFoundryAgentType,
  });

  const availableSecrets = secretsData?.items?.filter((secret) => secret.secret_type === 'foundry_client_secret') ?? [];

  // Fetch group permissions for the sub-agent
  const { data: groupPermissions } = useQuery({
    ...getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetOptions({
      path: { sub_agent_id: parseInt(id || '0', 10) },
    }),
    enabled: !!id,
  });

  // Fetch version history for all agent types
  const { data: versionHistoryData } = useQuery({
    ...getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGetOptions({
      path: { sub_agent_id: parseInt(id || '0', 10) },
    }),
    enabled: !!id,
  });

  // Fetch skill activations for this agent (personal/group)
  const { data: activationsData } = useQuery({
    ...listActivationsApiV1SkillsActivationsSubAgentIdGetOptions({
      path: { sub_agent_id: parseInt(id || '0', 10) },
    }),
    enabled: !!id,
  });
  const myActivations = activationsData?.items ?? [];

  // Fetch user's groups (for group scope activation)
  const { data: myGroupsData } = useQuery(listMyGroupsApiV1GroupsGetOptions());
  const myGroups = Array.isArray(myGroupsData) ? myGroupsData : [];

  // Activation mutations
  const invalidateActivations = () => {
    if (id) {
      queryClient.invalidateQueries({
        queryKey: listActivationsApiV1SkillsActivationsSubAgentIdGetQueryKey({
          path: { sub_agent_id: parseInt(id, 10) },
        }),
      });
    }
  };

  const activateSkillMutation = useMutation({
    ...activateSkillApiV1SkillsActivationsPostMutation(),
    onSuccess: () => {
      toast.success('Skill activated');
      invalidateActivations();
      setShowActivateSkillDialog(false);
    },
    onError: (error: any) => {
      const status = error?.status ?? error?.response?.status;
      if (status === 409) {
        toast.error('Skill is already activated on this agent');
      } else {
        toast.error('Failed to activate skill');
      }
    },
  });

  const deactivateSkillMutation = useMutation({
    ...deactivateSkillApiV1SkillsActivationsActivationIdDeleteMutation(),
    onSuccess: () => {
      toast.success('Skill deactivated');
      invalidateActivations();
      setDeactivatingActivation(null);
    },
    onError: () => toast.error('Failed to deactivate skill'),
  });

  const updateActivationMutation = useMutation({
    ...updateActivationApiV1SkillsActivationsActivationIdUpdatePostMutation(),
    onSuccess: () => {
      toast.success('Skill updated to latest');
      invalidateActivations();
    },
    onError: () => toast.error('Failed to update skill'),
  });
  // Sort versions in descending order (newest first)
  const versionHistory: SubAgentConfigVersion[] = (versionHistoryData || [])
    .slice()
    .sort((a, b) => (b.version ?? 0) - (a.version ?? 0));

  // Compute viewed version before the chat hook
  const currentVersion = subAgent?.current_version || 1;
  const isViewingHistoricalVersion = viewingVersionNumber !== null && viewingVersionNumber !== currentVersion;
  const viewedVersion = isViewingHistoricalVersion
    ? versionHistory.find((v: SubAgentConfigVersion) => v.version === viewingVersionNumber)
    : versionHistory.find((v: SubAgentConfigVersion) => v.version === currentVersion);

  // Playground chat hook - uses version hash for conversation filtering
  const {
    conversations,
    activeConversationId,
    isConnected,
    isLoading,
    isLoadingConversations: _isLoadingConversations,
    currentMessages: messages,
    createConversation: createNewConversation,
    selectConversation: handleSelectConversation,
    deleteConversation: handleDeleteConversation,
    sendMessage: sendPlaygroundMessage,
  } = usePlaygroundChat({
    subAgentConfigHash: viewedVersion?.version_hash || '',
    subAgentName: subAgent?.name || 'Unknown',
    configVersion: viewedVersion?.version || 0,
  });

  // Helper to invalidate sub-agent query
  const invalidateSubAgentQuery = () => {
    queryClient.invalidateQueries({
      predicate: (query) => {
        const key = query.queryKey[0];
        return (
          typeof key === 'object' &&
          key !== null &&
          '_id' in key &&
          key._id === 'getSubAgentApiV1SubAgentsSubAgentIdGet'
        );
      },
    });
    // Also invalidate version history
    queryClient.invalidateQueries({
      predicate: (query) => {
        const key = query.queryKey[0];
        return (
          typeof key === 'object' &&
          key !== null &&
          '_id' in key &&
          key._id === 'getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGet'
        );
      },
    });
  };

  // Mutations
  const updateMutation = useMutation({
    ...consoleUpdateSubAgentMutation(),
    onSuccess: () => {
      invalidateSubAgentQuery();
      setIsEditing(false);
      setHasUnsavedChanges(false);
      toast.success('Configuration saved successfully');
    },
    onError: (err) => {
      toast.error('Failed to save configuration', { description: getErrorMessage(err) });
    },
  });

  const deleteMutation = useMutation({
    ...deleteSubAgentApiV1SubAgentsSubAgentIdDeleteMutation(),
    onSuccess: () => {
      toast.success('Sub-agent deleted successfully');
      navigate('/app/subagents');
    },
    onError: (err) => {
      toast.error('Failed to delete sub-agent', { description: getErrorMessage(err) });
    },
  });

  const submitMutation = useMutation({
    ...submitForApprovalApiV1SubAgentsSubAgentIdSubmitPostMutation(),
    onSuccess: () => {
      invalidateSubAgentQuery();
      toast.success('Sub-agent submitted for approval');
    },
    onError: (err) => {
      toast.error('Failed to submit for approval', { description: getErrorMessage(err) });
    },
  });

  const reviewVersionMutation = useMutation({
    ...reviewVersionApiV1SubAgentsSubAgentIdVersionsVersionReviewPostMutation(),
    onSuccess: () => {
      invalidateSubAgentQuery();
      setApprovalAction(null);
      toast.success('Version review completed');
    },
    onError: (err) => {
      toast.error('Failed to process review action', { description: getErrorMessage(err) });
    },
  });

  const systemRoleMutation = useMutation({
    mutationFn: async (role: string | null) => {
      const response = await client.put({
        url: '/api/v1/sub-agents/{sub_agent_id}/system-role',
        path: { sub_agent_id: id! },
        query: role ? { role } : {},
      });
      if (response.error) throw new Error('Failed to set system role');
      return response.data;
    },
    onSuccess: () => {
      invalidateSubAgentQuery();
      toast.success('System role updated');
    },
    onError: (err) => {
      toast.error('Failed to update system role', { description: getErrorMessage(err) });
    },
  });

  const currentUserId = user?.id ?? '';
  const isOwner = subAgent?.owner_user_id === currentUserId;
  const isAdministrator = user?.is_administrator ?? false;
  const isApproverRole = user?.role === 'approver' || user?.role === 'admin' || isAdministrator;
  const canApprove = isApproverRole;

  // Sub-agent overall status (from config_version)
  const subAgentStatus = (subAgent?.config_version?.status ?? 'draft') as SubAgentStatus;

  // Get the current version's status (may differ from sub-agent status)
  const currentVersionData = versionHistory.find((v: SubAgentConfigVersion) => v.version === currentVersion);
  const currentVersionStatus = (currentVersionData?.status ?? subAgentStatus) as SubAgentStatus;

  // For header status display, always use current version's status (not the viewed version)
  const status: SubAgentStatus = currentVersionStatus;

  // Check if user has write access through any of their groups
  const hasGroupWriteAccess = (() => {
    if (!user?.groups || !groupPermissions) return false;

    // Get user's group IDs
    const userGroupIds = new Set(user.groups.map((g) => g.id));

    // Check if any of the sub-agent's group permissions grant write access
    // to a group the user belongs to
    return groupPermissions.some((perm) => {
      if (!userGroupIds.has(perm.user_group_id)) return false;

      // Check if the permission includes 'write'
      const hasWritePermission = perm.permissions.includes('write');
      if (!hasWritePermission) return false;

      // Find the user's role in this group
      const userGroup = user.groups?.find((g) => g.id === perm.user_group_id);
      if (!userGroup) return false;

      // User must have 'write' or 'manager' role in the group to actually write
      // (read-only group members can't write even if the group has write permission)
      return userGroup.group_role === 'write' || userGroup.group_role === 'manager';
    });
  })();

  // Owners can edit at any status - but only when viewing current version
  // Administrators with admin mode enabled can also edit
  // Users with write access through groups can also edit
  const canEdit = (isOwner || (isAdministrator && adminMode) || hasGroupWriteAccess) && !isViewingHistoricalVersion;
  const canDelete = isOwner || canApprove;
  // Can submit if owner or has write access through groups, and current version is draft
  const canSubmitForApproval = (isOwner || hasGroupWriteAccess) && currentVersionStatus === 'draft';

  // Left panel tab: owners/writers default to config, everyone else to personalize
  // GP agent (system_role='general-purpose') always shows personalize (no configurable settings)
  const isGpAgent = subAgent?.system_role === 'general-purpose';
  const [leftPanelTab, setLeftPanelTab] = useState<'config' | 'personalize'>('config');
  const leftPanelTabInitialized = useRef(false);
  useEffect(() => {
    if (!leftPanelTabInitialized.current && subAgent) {
      leftPanelTabInitialized.current = true;
      setLeftPanelTab(isGpAgent ? 'personalize' : canEdit ? 'config' : 'personalize');
    }
  }, [subAgent, canEdit, isGpAgent]);

  // Get displayed data based on whether viewing historical version
  const displayedDescription = isViewingHistoricalVersion
    ? (viewedVersion?.description ?? subAgent?.config_version?.description ?? '')
    : (subAgent?.config_version?.description ?? '');
  const displayedModel = isViewingHistoricalVersion
    ? (viewedVersion?.model ?? subAgent?.config_version?.model ?? '')
    : (subAgent?.config_version?.model ?? '');
  // Model lifecycle, resolved by console-backend (single source of truth for retirement).
  const displayedConfigVersion = isViewingHistoricalVersion
    ? (viewedVersion ?? subAgent?.config_version)
    : subAgent?.config_version;
  const displayedModelRetired = displayedConfigVersion?.model_retired ?? false;
  const displayedEffectiveModel = displayedConfigVersion?.effective_model ?? null;
  // Tier binding (mutually exclusive with a concrete model). When set, the agent shows/runs
  // the tier's current default rather than a fixed alias.
  const displayedModelTier = displayedConfigVersion?.model_tier ?? null;
  // The inline editor stores model OR tier in one value: a tier is `tier:<tier>`.
  const editIsTier = editModel.startsWith('tier:');
  const editModelAlias = editIsTier ? '' : editModel;
  const editModelTier = editIsTier ? editModel.slice('tier:'.length) : null;
  const displayedSystemPrompt = isViewingHistoricalVersion
    ? (viewedVersion?.system_prompt ?? subAgent?.config_version?.system_prompt ?? '')
    : (subAgent?.config_version?.system_prompt ?? '');
  const displayedAgentUrl = isViewingHistoricalVersion
    ? (viewedVersion?.agent_url ?? subAgent?.config_version?.agent_url ?? '')
    : (subAgent?.config_version?.agent_url ?? '');
  const displayedMcpTools = isViewingHistoricalVersion
    ? (viewedVersion?.mcp_tools ?? subAgent?.config_version?.mcp_tools ?? [])
    : (subAgent?.config_version?.mcp_tools ?? []);
  const displayedFoundryHostname = isViewingHistoricalVersion
    ? (viewedVersion?.foundry_hostname ?? subAgent?.config_version?.foundry_hostname ?? '')
    : (subAgent?.config_version?.foundry_hostname ?? '');
  const displayedFoundryClientId = isViewingHistoricalVersion
    ? (viewedVersion?.foundry_client_id ?? subAgent?.config_version?.foundry_client_id ?? '')
    : (subAgent?.config_version?.foundry_client_id ?? '');
  const displayedFoundryClientSecretRef = isViewingHistoricalVersion
    ? (viewedVersion?.foundry_client_secret_ref ?? subAgent?.config_version?.foundry_client_secret_ref ?? null)
    : (subAgent?.config_version?.foundry_client_secret_ref ?? null);
  const displayedFoundryOntologyRid = isViewingHistoricalVersion
    ? (viewedVersion?.foundry_ontology_rid ?? subAgent?.config_version?.foundry_ontology_rid ?? '')
    : (subAgent?.config_version?.foundry_ontology_rid ?? '');
  const displayedFoundryQueryApiName = isViewingHistoricalVersion
    ? (viewedVersion?.foundry_query_api_name ?? subAgent?.config_version?.foundry_query_api_name ?? '')
    : (subAgent?.config_version?.foundry_query_api_name ?? '');
  const displayedFoundryScopes = isViewingHistoricalVersion
    ? (viewedVersion?.foundry_scopes ?? subAgent?.config_version?.foundry_scopes ?? [])
    : (subAgent?.config_version?.foundry_scopes ?? []);
  const displayedFoundryVersion = isViewingHistoricalVersion
    ? (viewedVersion?.foundry_version ?? subAgent?.config_version?.foundry_version ?? '')
    : (subAgent?.config_version?.foundry_version ?? '');
  const displayedSkills = isViewingHistoricalVersion
    ? (viewedVersion?.skills ?? subAgent?.config_version?.skills ?? [])
    : (subAgent?.config_version?.skills ?? []);
  const displayedSandboxEnabled = isViewingHistoricalVersion
    ? (viewedVersion?.sandbox_enabled ?? subAgent?.config_version?.sandbox_enabled ?? false)
    : (subAgent?.config_version?.sandbox_enabled ?? false);
  const displayedEnableThinking = isViewingHistoricalVersion
    ? (viewedVersion?.enable_thinking ?? subAgent?.config_version?.enable_thinking ?? false)
    : (subAgent?.config_version?.enable_thinking ?? false);
  const displayedThinkingLevel = isViewingHistoricalVersion
    ? (viewedVersion?.thinking_level ?? subAgent?.config_version?.thinking_level ?? null)
    : (subAgent?.config_version?.thinking_level ?? null);

  // Get active conversation
  const activeConversation = conversations.find((c) => c.id === activeConversationId);

  useEffect(() => {
    if (subAgent) {
      initEditState(subAgent);
    }
  }, [subAgent]);

  // Check for available updates on imported skills when entering edit mode
  useEffect(() => {
    if (!isEditing) {
      setSkillsWithUpdates(new Set());
      return;
    }
    // Use backend-provided update_available flag
    const updatable = new Set<string>();
    for (const skill of editSkills) {
      if (skill.registry_id && skill.update_available) {
        updatable.add(skill.name!);
      }
    }
    if (updatable.size > 0) {
      setSkillsWithUpdates(updatable);
      return;
    }
    // Fallback: check registry for imported skills without update_available flag
    const importedSkills = editSkills.filter((s) => s.registry_id && s.content_hash && !s.update_available && s.scope !== 'sub-agent');
    if (importedSkills.length === 0) return;

    let cancelled = false;
    (async () => {
      for (const skill of importedSkills) {
        try {
          const { data } = await client.get({
            url: '/api/v1/skills/registry/detail/{skill_id}',
            path: { skill_id: skill.registry_id! },
          });
          if (cancelled) return;
          const detail = data as { content_hash?: string } | undefined;
          if (detail?.content_hash && detail.content_hash !== skill.content_hash) {
            updatable.add(skill.name!);
          }
        } catch {
          // Skip check if registry is unreachable
        }
      }
      if (!cancelled) setSkillsWithUpdates(updatable);
    })();
    return () => { cancelled = true; };
  }, [isEditing, editSkills.length]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Screen width detection for responsive behavior
  useEffect(() => {
    const handleResize = () => setScreenWidth(window.innerWidth);
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Focus-aware auto-expansion with debouncing
  useEffect(() => {
    if (layoutLocked || screenWidth < 1400) return;

    // Debounce focus changes to prevent rapid switching
    if (focusTimeoutRef.current) {
      clearTimeout(focusTimeoutRef.current);
    }

    focusTimeoutRef.current = setTimeout(() => {
      if (activeFocusArea === 'config') {
        // Expand config panel for comfortable editing
        setConfigPanelWidth('wide');
        localStorage.setItem('subagent-config-panel-width', 'wide');
        // Auto-close conversation list on smaller screens
        if (screenWidth < 1600 && showConversationList) {
          setShowConversationList(false);
        }
      } else if (activeFocusArea === 'chat') {
        // Shrink config panel to maximize chat space
        setConfigPanelWidth('compact');
        localStorage.setItem('subagent-config-panel-width', 'compact');
      } else if (activeFocusArea === 'version') {
        // Medium config when viewing version history
        setConfigPanelWidth('medium');
        localStorage.setItem('subagent-config-panel-width', 'medium');
        // Auto-close conversation list on smaller screens
        if (screenWidth < 1800 && showConversationList) {
          setShowConversationList(false);
        }
      }
    }, 300) as unknown as number;

    return () => {
      if (focusTimeoutRef.current) {
        clearTimeout(focusTimeoutRef.current);
      }
    };
  }, [activeFocusArea, layoutLocked, screenWidth, showConversationList]);

  // Screen width detection for responsive behavior
  useEffect(() => {
    const handleResize = () => setScreenWidth(window.innerWidth);
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  const initEditState = (sa: typeof subAgent) => {
    if (!sa) return;
    setEditName(sa.name);
    setEditIsPublic(sa.is_public ?? false);
    setEditDescription(sa.config_version?.description || '');
    setEditModel(sa.config_version?.model_tier ? `tier:${sa.config_version.model_tier}` : (sa.config_version?.model || ''));
    if (sa.type === 'remote') {
      setEditAgentUrl(String(sa.config_version?.agent_url ?? ''));
    } else if (sa.type === 'foundry') {
      setEditFoundryHostname(String(sa.config_version?.foundry_hostname ?? ''));
      setEditFoundryClientId(String(sa.config_version?.foundry_client_id ?? ''));
      setEditFoundryClientSecretRef(sa.config_version?.foundry_client_secret_ref ?? null);
      setEditFoundryOntologyRid(String(sa.config_version?.foundry_ontology_rid ?? ''));
      setEditFoundryQueryApiName(String(sa.config_version?.foundry_query_api_name ?? ''));
      setEditFoundryScopes(Array.isArray(sa.config_version?.foundry_scopes) ? sa.config_version.foundry_scopes : []);
      setEditFoundryVersion(String(sa.config_version?.foundry_version ?? ''));
    } else {
      setEditSystemPrompt(String(sa.config_version?.system_prompt ?? ''));
      setEditMcpTools(Array.isArray(sa.config_version?.mcp_tools) ? sa.config_version.mcp_tools : []);
      setEditEnableThinking(sa.config_version?.enable_thinking ?? false);
      setEditThinkingLevel((sa.config_version?.thinking_level as OrchestratorThinkingLevel) ?? null);
    }

    // Initialize skills and sandbox for local agents
    if (sa.type === 'local') {
      const cfgSkills = sa.config_version?.skills;
      setEditSkills(
        Array.isArray(cfgSkills)
          ? cfgSkills.map((s: any) => ({
              name: s.name,
              description: s.description,
              body: s.body,
              files: s.files?.map((f: McpSkillFile) => ({ path: f.path, content: f.content })),
              registry_id: s.registry_id ?? null,
              source: s.source ?? null,
              content_hash: s.content_hash ?? null,
              scope: s.scope ?? null,
            }))
          : []
      );
      setEditSandboxEnabled(sa.config_version?.sandbox_enabled ?? false);
    }

    // Initialize pricing config for remote and foundry agents
    if (sa.type === 'remote' || sa.type === 'foundry') {
      const pricingConfig = sa.config_version?.pricing_config as any;
      if (pricingConfig?.rate_card_entries && pricingConfig.rate_card_entries.length > 0) {
        setEditRateCardEntries(
          pricingConfig.rate_card_entries.map((e: any) => ({
            billing_unit: e.billing_unit,
            price_per_million: e.price_per_million != null ? e.price_per_million.toString() : '',
          }))
        );
      } else {
        // Default to requests billing unit
        setEditRateCardEntries([{ billing_unit: 'requests', price_per_million: '' }]);
      }
    }
  };

  const handleSave = async () => {
    // Show change summary dialog instead of saving directly
    setShowChangeSummaryDialog(true);
  };

  const handleSaveWithSummary = async (summary: string) => {
    if (!subAgent || !id) return;

    let typeSpecificConfig: any = {};
    if (subAgent.type === 'remote') {
      typeSpecificConfig = { agent_url: editAgentUrl };
      // Add pricing config for remote agents (only detailed format supported)
      if (editRateCardEntries.length > 0) {
        typeSpecificConfig.pricing_config = {
          format: 'detailed',
          rate_card_entries: editRateCardEntries.map((e) => ({
            billing_unit: e.billing_unit,
            price_per_million: parseFloat(e.price_per_million),
          })),
        };
      }
    } else if (subAgent.type === 'foundry') {
      typeSpecificConfig = {
        foundry_hostname: editFoundryHostname,
        foundry_client_id: editFoundryClientId,
        foundry_client_secret_ref: editFoundryClientSecretRef,
        foundry_ontology_rid: editFoundryOntologyRid,
        foundry_query_api_name: editFoundryQueryApiName,
        foundry_scopes: editFoundryScopes as any,
        ...(editFoundryVersion && { foundry_version: editFoundryVersion }),
      };
      // Add pricing config for foundry agents (only detailed format supported)
      if (editRateCardEntries.length > 0) {
        typeSpecificConfig.pricing_config = {
          format: 'detailed',
          rate_card_entries: editRateCardEntries.map((e) => ({
            billing_unit: e.billing_unit,
            price_per_million: parseFloat(e.price_per_million),
          })),
        };
      }
    } else {
      typeSpecificConfig = {
        system_prompt: editSystemPrompt,
        mcp_tools: editMcpTools.length > 0 ? editMcpTools : undefined,
        enable_thinking: editEnableThinking,
        thinking_level: editThinkingLevel ?? undefined, // Convert null to undefined for API
        skills: editSkills,
        sandbox_enabled: editSandboxEnabled,
      };
    }

    updateMutation.mutate({
      path: { sub_agent_id: parseInt(id, 10) },
      body: {
        name: editName,
        is_public: editIsPublic,
        description: editDescription,
        model: editIsTier ? undefined : (editModelAlias || undefined),
        model_tier: editIsTier ? (editModelTier as ModelTier) : undefined,
        ...typeSpecificConfig,
        change_summary: summary || 'Updated configuration from playground',
      },
    });

    setShowChangeSummaryDialog(false);
    setChangeSummary('');
  };

  const handleCancelEdit = () => {
    if (subAgent) {
      initEditState(subAgent);
    }
    setIsEditing(false);
    setHasUnsavedChanges(false);
  };

  const handleFieldChange = () => {
    setHasUnsavedChanges(true);
  };

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
      const detail = data as { name?: string; slug?: string; description?: string; content_hash?: string; scope?: string };
      const newSkill = {
        name: detail.slug ?? skill.slug ?? skill.name,
        description: detail.description ?? '',
        body: '',
        files: undefined,
        registry_id: skill.id,
        content_hash: detail.content_hash ?? null,
        scope: 'standalone' as const,
      };
      // Don't add duplicates
      if (editSkills.some((s) => s.name === newSkill.name)) {
        toast.error(`Skill "${newSkill.name}" is already added`);
        return;
      }
      setEditSkills((prev) => [...prev, newSkill]);
      handleFieldChange();
      toast.success(`Imported "${newSkill.name}"`);
      setIsSkillImportOpen(false);
    } finally {
      setImportingSkillId(null);
    }
  };

  const handleUpdateImportedSkill = async (skillName: string, registryId: string) => {
    setUpdatingSkillName(skillName);
    try {
      const { data, error } = await client.get({
        url: '/api/v1/skills/registry/detail/{skill_id}',
        path: { skill_id: registryId },
      });
      if (error || !data) {
        toast.error('Failed to fetch latest version from registry');
        return false;
      }
      const detail = data as { name?: string; slug?: string; description?: string; content_hash?: string };
      setEditSkills((prev) =>
        prev.map((s) =>
          s.name === skillName
            ? { ...s, description: detail.description ?? s.description, content_hash: detail.content_hash ?? s.content_hash }
            : s
        )
      );
      setSkillsWithUpdates((prev) => {
        const next = new Set(prev);
        next.delete(skillName);
        return next;
      });
      handleFieldChange();
      toast.success(`Updated "${skillName}" to latest version`);
      return true;
    } catch (error) {
      console.error('Failed to update skill:', error);
      toast.error('Failed to update skill. Please try again.');
      return false;
    } finally {
      setUpdatingSkillName(null);
    }
  };

  const handleConfirmSkillDiffUpdate = async () => {
    if (!skillDiffInfo?.updateTarget) return;

    if (skillDiffInfo.updateTarget.type === 'imported-skill') {
      const updated = await handleUpdateImportedSkill(skillDiffInfo.updateTarget.skillName, skillDiffInfo.registryId);
      if (updated) setSkillDiffInfo(null);
      return;
    }

    if (skillDiffInfo.updateTarget.type === 'imported-skill-direct') {
      // Direct save without entering edit mode — fetch latest and persist immediately
      const skillName = skillDiffInfo.updateTarget.skillName;
      setUpdatingSkillName(skillName);
      try {
        const { data, error } = await client.get({
          url: '/api/v1/skills/registry/detail/{skill_id}',
          path: { skill_id: skillDiffInfo.registryId },
        });
        if (error || !data) {
          toast.error('Failed to fetch latest version from registry');
          return;
        }
        const detail = data as { description?: string; content_hash?: string };
        const currentSkills: SkillDefinition[] = Array.isArray(subAgent?.config_version?.skills)
          ? subAgent!.config_version!.skills.map((s: any) => ({
              name: s.name,
              description: s.description,
              body: s.body,
              files: s.files?.map((f: any) => ({ path: f.path, content: f.content })),
              registry_id: s.registry_id ?? null,
              source: s.source ?? null,
              content_hash: s.content_hash ?? null,
              scope: s.scope ?? null,
            }))
          : [];
        const updatedSkills = currentSkills.map((s) =>
          s.name === skillName
            ? { ...s, description: detail.description ?? s.description, content_hash: detail.content_hash ?? s.content_hash }
            : s
        );
        updateMutation.mutate({
          path: { sub_agent_id: parseInt(id!, 10) },
          body: {
            name: subAgent!.name,
            is_public: subAgent!.is_public ?? false,
            description: subAgent!.config_version?.description || '',
            model: (subAgent!.config_version?.model as any) || undefined,
            system_prompt: subAgent!.config_version?.system_prompt ?? '',
            mcp_tools: (subAgent!.config_version?.mcp_tools as string[] | undefined)?.length ? subAgent!.config_version!.mcp_tools as string[] : undefined,
            enable_thinking: subAgent!.config_version?.enable_thinking ?? false,
            thinking_level: (subAgent!.config_version?.thinking_level as any) ?? undefined,
            skills: updatedSkills,
            sandbox_enabled: subAgent!.config_version?.sandbox_enabled ?? false,
            change_summary: `Updated skill "${skillName}" to latest version`,
          },
        });
        setSkillDiffInfo(null);
      } catch (err) {
        console.error('Failed to directly update skill:', err);
        toast.error('Failed to update skill. Please try again.');
      } finally {
        setUpdatingSkillName(null);
      }
      return;
    }

    try {
      await updateActivationMutation.mutateAsync({ path: { activation_id: skillDiffInfo.updateTarget.activationId } });
      setSkillDiffInfo(null);
    } catch (error) {
      // mutation onError handles user-facing toast
      console.error('Failed to update activation from diff dialog', error);
    }
  };

  const isSkillDiffUpdatePending = useMemo(() => {
    if (!skillDiffInfo?.updateTarget) return false;
    if (skillDiffInfo.updateTarget.type === 'imported-skill') {
      return updatingSkillName === skillDiffInfo.updateTarget.skillName;
    }
    if (skillDiffInfo.updateTarget.type === 'imported-skill-direct') {
      return updatingSkillName === skillDiffInfo.updateTarget.skillName || updateMutation.isPending;
    }
    return updateActivationMutation.isPending;
  }, [skillDiffInfo, updatingSkillName, updateActivationMutation.isPending, updateMutation.isPending]);

  const handleNewConversation = () => {
    createNewConversation(currentVersion || 1);
  };

  const handleSendMessage = async () => {
    if (!inputValue.trim() || isLoading || !isConnected) return;

    const content = inputValue.trim();
    setInputValue('');

    await sendPlaygroundMessage(content, currentVersion || 1);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  const handleDelete = async () => {
    if (!id) return;
    deleteMutation.mutate({
      path: { sub_agent_id: parseInt(id, 10) },
    });
  };

  const handleSubmitForApproval = async () => {
    if (!id || !submitChangeSummary.trim()) return;
    submitMutation.mutate({
      path: { sub_agent_id: parseInt(id, 10) },
      body: { change_summary: submitChangeSummary.trim() },
    });
    setShowSubmitDialog(false);
    setSubmitChangeSummary('');
  };

  const handleApprovalAction = async (action: 'approve' | 'reject', rejectionReason?: string) => {
    if (!id || !currentVersion) return;
    // Use version-specific review endpoint for consistency
    reviewVersionMutation.mutate({
      path: {
        sub_agent_id: parseInt(id, 10),
        version: currentVersion,
      },
      body: {
        action: action,
        rejection_reason: action === 'reject' ? rejectionReason : undefined,
      },
    });
  };

  const isSubmitting = updateMutation.isPending || submitMutation.isPending || reviewVersionMutation.isPending;

  if (!subAgent) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4 p-4">
        <AlertCircle className="h-12 w-12 text-muted-foreground" />
        <p className="text-muted-foreground">Sub-agent not found</p>
        <Button variant="outline" onClick={() => navigate('/app/subagents')}>
          Back to Sub-Agents
        </Button>
      </div>
    );
  }

  const TypeIcon = subAgent.type === 'remote' ? Globe : subAgent.type === 'foundry' ? Database : Terminal;
  const statusInfo = statusConfig[status];
  const StatusIcon = statusInfo.icon;

  // Determine if sub-agent is live in production (has a default version)
  const isLive = subAgent.default_version !== null && subAgent.default_version !== undefined;
  const liveVersion = subAgent.default_version;
  const liveVersionData = versionHistory.find((v: SubAgentConfigVersion) => v.version === liveVersion);

  // Helper function to format version display
  const formatVersionLabel = (version: SubAgentConfigVersion | undefined, fallbackVersion?: number | null): string => {
    if (!version) {
      return fallbackVersion != null ? `v${fallbackVersion}` : '';
    }
    // For approved versions, show release number (e.g., "v1", "v2")
    if (version.status === 'approved' && version.release_number) {
      return `v${version.release_number}`;
    }
    // For draft/pending versions, show hash (e.g., "#a1b2c3d")
    if (version.version_hash) {
      return `#${version.version_hash.slice(0, 7)}`;
    }
    // Fallback to version number
    return `v${version.version}`;
  };

  // Helper to get formatted label for a version number (looks up in history)
  const getVersionLabel = (versionNum: number | null | undefined): string => {
    if (versionNum == null) return '';
    const versionData = versionHistory.find((v: SubAgentConfigVersion) => v.version === versionNum);
    return formatVersionLabel(versionData, versionNum);
  };

  // Show current version status if it differs from the live version
  const showCurrentVersionStatus = currentVersion !== liveVersion;

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)] p-4">
      {/* Header with Status and Actions */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <FlaskConical className="h-5 w-5 text-amber-600" />
          <div>
            <div className="flex items-center gap-2">
              <span className="font-medium">{subAgent.name}</span>
              {/* Show live/production status if applicable */}
              {isLive && (
                <Badge variant="default">
                  <CheckCircle className="mr-1 h-3 w-3" />
                  {formatVersionLabel(liveVersionData, liveVersion)} Live
                </Badge>
              )}
              {/* Show current version status if different from live */}
              {showCurrentVersionStatus && (
                <>
                  {isLive && <span className="text-muted-foreground">•</span>}
                  <Badge variant={statusInfo.variant}>
                    <StatusIcon className="mr-1 h-3 w-3" />
                    {formatVersionLabel(currentVersionData, currentVersion)}: {statusInfo.label}
                  </Badge>
                </>
              )}
              {/* If not live and not showing version status, show simple status */}
              {!isLive && !showCurrentVersionStatus && (
                <Badge variant={statusInfo.variant}>
                  <StatusIcon className="mr-1 h-3 w-3" />
                  {statusInfo.label}
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <TypeIcon className="h-3 w-3" />
              <span className="capitalize">{subAgent.type} Agent</span>
              <span>•</span>
              <span>by {subAgent.owner?.name || 'Unknown'}</span>
              {subAgent.system_role && !(isAdministrator && adminMode) && (
                <>
                  <span>•</span>
                  <Badge variant="outline" className="text-xs font-normal">
                    <Wrench className="mr-1 h-3 w-3" />
                    {subAgent.system_role}
                  </Badge>
                </>
              )}
              {isAdministrator && adminMode && (
                <>
                  <span>•</span>
                  <Select
                    value={subAgent.system_role || '__none__'}
                    onValueChange={(value) => systemRoleMutation.mutate(value === '__none__' ? null : value)}
                  >
                    <SelectTrigger className="h-6 w-[130px] text-xs">
                      <SelectValue placeholder="System role" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__none__">No system role</SelectItem>
                      <SelectItem value="general-purpose">General Purpose</SelectItem>
                      <SelectItem value="assessor">Assessor</SelectItem>
                      <SelectItem value="debug">Debug</SelectItem>
                    </SelectContent>
                  </Select>
                </>
              )}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {canSubmitForApproval && (
            <Button onClick={() => setShowSubmitDialog(true)} disabled={isSubmitting}>
              {isSubmitting ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Send className="h-4 w-4 mr-2" />}
              Submit for Approval
            </Button>
          )}
          {canApprove && status === 'pending_approval' && (
            <>
              <Button variant="outline" onClick={() => setApprovalAction('reject')}>
                <XCircle className="mr-2 h-4 w-4" />
                Reject
              </Button>
              <Button onClick={() => setApprovalAction('approve')}>
                <CheckCircle className="mr-2 h-4 w-4" />
                Approve
              </Button>
            </>
          )}
          {canDelete && (
            <Button variant="outline" size="icon" onClick={() => setShowDeleteDialog(true)}>
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {/* Status Alerts */}
      {showCurrentVersionStatus && isLive && (
        <Alert className="mb-4 border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950/50">
          <Info className="h-4 w-4 text-blue-600" />
          <AlertDescription className="text-blue-700 dark:text-blue-400">
            This sub-agent is live with {formatVersionLabel(liveVersionData, liveVersion)}, but you're viewing{' '}
            {formatVersionLabel(currentVersionData, currentVersion)} which is{' '}
            {status === 'draft' ? 'a draft' : status === 'pending_approval' ? 'pending approval' : status}.
            {status === 'draft' && ' Submit for approval when ready.'}
          </AlertDescription>
        </Alert>
      )}

      {status === 'pending_approval' && !canApprove && (
        <Alert className="mb-4 border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/50">
          <Clock className="h-4 w-4 text-amber-600" />
          <AlertDescription className="text-amber-700 dark:text-amber-400">
            This sub-agent is pending approval. An admin will review it shortly.
          </AlertDescription>
        </Alert>
      )}

      {status === 'rejected' && subAgent.config_version?.rejection_reason && (
        <Alert variant="destructive" className="mb-4">
          <XCircle className="h-4 w-4" />
          <AlertDescription>
            <strong>Rejected:</strong> {subAgent.config_version.rejection_reason}
          </AlertDescription>
        </Alert>
      )}

      {/* Main Content - Split View */}
      <div className="flex-1 flex gap-4 min-h-0">
        {/* Left Column - Configuration & Group Access */}
        <div
          className={`flex flex-col gap-4 flex-shrink-0 ${
            configPanelWidth === 'compact' ? 'w-[400px]' : configPanelWidth === 'wide' ? 'w-[800px]' : 'w-[560px]'
          }`}
        >
          {/* Main Panel with Tabs */}
          <Tabs value={leftPanelTab} onValueChange={(v) => setLeftPanelTab(v as 'config' | 'personalize')} className="flex flex-col flex-1 min-h-0">
          <div
            className={`flex flex-col rounded-lg border overflow-hidden flex-1 min-h-0 motion-safe:transition-all motion-safe:duration-300 motion-safe:ease-in-out ${
              isEditing && leftPanelTab === 'config' ? 'border-amber-500/50 bg-amber-50/30 dark:bg-amber-950/20' : 'border-border bg-muted/30'
            }`}
          >
            <div className="flex items-center justify-between px-4 py-2 border-b border-border shrink-0">
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => navigate('/app/subagents')}>
                  <ArrowLeft className="h-4 w-4" />
                </Button>
                <TabsList className="h-8">
                  {!isGpAgent && (
                    <TabsTrigger value="config" className="text-xs px-3 h-6">
                      Configuration
                    </TabsTrigger>
                  )}
                  <TabsTrigger value="personalize" className="text-xs px-3 h-6">
                    My Skills
                  </TabsTrigger>
                </TabsList>
                {leftPanelTab === 'config' && isEditing && (
                  <Badge
                    variant="outline"
                    className="text-xs border-amber-500 text-amber-600 bg-amber-50 dark:bg-amber-950"
                  >
                    <Edit className="mr-1 h-3 w-3" />
                    Editing
                  </Badge>
                )}
                {leftPanelTab === 'config' && isViewingHistoricalVersion && (
                  <Badge variant="outline" className="text-xs border-amber-500 text-amber-600">
                    {formatVersionLabel(viewedVersion, viewingVersionNumber)}
                  </Badge>
                )}
              </div>
              {leftPanelTab === 'config' && (
              <div className="flex items-center gap-1">
                {/* Layout lock toggle */}
                {!isViewingHistoricalVersion && (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => {
                            const newLocked = !layoutLocked;
                            setLayoutLocked(newLocked);
                            localStorage.setItem('subagent-layout-locked', String(newLocked));
                          }}
                        >
                          {layoutLocked ? <Lock className="h-4 w-4 text-amber-600" /> : <Unlock className="h-4 w-4" />}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        <p>
                          {layoutLocked
                            ? 'Layout locked - click to enable auto-resize'
                            : 'Auto-resize enabled - click to lock'}
                        </p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
                {/* Panel width controls */}
                {!isViewingHistoricalVersion && (
                  <Select
                    value={configPanelWidth}
                    onValueChange={(value: 'compact' | 'medium' | 'wide') => {
                      setConfigPanelWidth(value);
                      localStorage.setItem('subagent-config-panel-width', value);
                    }}
                  >
                    <SelectTrigger className="h-7 w-7 p-0 border-0" title="Adjust panel width">
                      <Maximize2 className="h-4 w-4" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="compact">Compact (400px)</SelectItem>
                      <SelectItem value="medium">Medium (560px)</SelectItem>
                      <SelectItem value="wide">Wide (800px)</SelectItem>
                    </SelectContent>
                  </Select>
                )}
                {canEdit && !isViewingHistoricalVersion && (
                  <>
                    {isEditing ? (
                      <>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              onClick={handleCancelEdit}
                              disabled={updateMutation.isPending}
                              className="h-7 w-7"
                            >
                              <X className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>Cancel editing</TooltipContent>
                        </Tooltip>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              size="icon"
                              onClick={handleSave}
                              disabled={updateMutation.isPending}
                              className="h-7 w-7"
                            >
                              {updateMutation.isPending ? (
                                <Loader2 className="h-4 w-4 animate-spin" />
                              ) : (
                                <Save className="h-4 w-4" />
                              )}
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>Save changes</TooltipContent>
                        </Tooltip>
                      </>
                    ) : (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => {
                              setIsEditing(true);
                              setActiveFocusArea('config');
                              setShowConversationList(false);
                            }}
                            className="h-7 w-7"
                          >
                            <Edit className="h-4 w-4" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Edit configuration</TooltipContent>
                      </Tooltip>
                    )}
                  </>
                )}
              </div>
              )}
              {leftPanelTab === 'personalize' && (
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setShowActivateSkillDialog(true)}
              >
                <Plus className="h-3 w-3 mr-1" />
                Add Skill
              </Button>
              )}
            </div>

            <TabsContent value="config" className="flex-1 min-h-0 flex flex-col mt-0 data-[state=inactive]:hidden">
            {/* Read-only mode indicator */}
            {isViewingHistoricalVersion && (
              <div className="px-3 py-2 bg-amber-500/10 border-b border-amber-500/20 flex items-center justify-between shrink-0">
                <div className="flex items-center gap-2 text-xs text-amber-600 dark:text-amber-400">
                  <Info className="h-3.5 w-3.5" />
                  <span>Viewing {formatVersionLabel(viewedVersion, viewingVersionNumber)} (read-only)</span>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 text-xs text-amber-600 dark:text-amber-400 hover:text-amber-700"
                  onClick={() => setViewingVersionNumber(null)}
                >
                  Back to current
                </Button>
              </div>
            )}

            <ScrollArea className="flex-1 min-h-0">
              <div className="p-3 flex flex-col gap-3 h-full w-full max-w-full">
                {/* Section: Identity */}
                <ConfigSection title="Identity" icon={FileText}>
                  <div className="space-y-1.5">
                    <Label htmlFor="name" className="text-xs">Name</Label>
                    {isEditing ? (
                      <Input
                        id="name"
                        value={editName}
                        onChange={(e) => {
                          setEditName(e.target.value);
                          handleFieldChange();
                        }}
                        onFocus={() => setActiveFocusArea('config')}
                        onBlur={() => setActiveFocusArea(null)}
                        className="h-8 text-sm"
                      />
                    ) : (
                      <p className="text-sm">{subAgent.name}</p>
                    )}
                  </div>

                  <div className="space-y-1.5">
                    <div className="flex items-center gap-2">
                      <Label htmlFor="description" className="text-xs">Description</Label>
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <HelpCircle className="h-3 w-3 text-muted-foreground cursor-help" />
                          </TooltipTrigger>
                          <TooltipContent className="max-w-xs">
                            <p>
                              The orchestrator uses this description to route conversations to the appropriate sub-agent.
                            </p>
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    </div>
                    {isEditing ? (
                      <Textarea
                        id="description"
                        value={editDescription}
                        onChange={(e) => {
                          setEditDescription(e.target.value);
                          handleFieldChange();
                        }}
                        onFocus={() => setActiveFocusArea('config')}
                        onBlur={() => setActiveFocusArea(null)}
                        rows={3}
                        className="text-sm resize-none"
                        placeholder="Describe what this sub-agent does..."
                      />
                    ) : (
                      <p className="text-sm text-muted-foreground">{displayedDescription || 'No description'}</p>
                    )}
                  </div>

                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label htmlFor="is_public" className="text-xs flex items-center gap-1.5">
                        <Users className="h-3 w-3" />
                        Public Access
                      </Label>
                      <p className="text-[11px] text-muted-foreground">
                        All users can access without group permissions
                      </p>
                    </div>
                    {isEditing ? (
                      <Switch
                        id="is_public"
                        checked={editIsPublic}
                        onCheckedChange={(checked) => {
                          setEditIsPublic(checked);
                          handleFieldChange();
                        }}
                      />
                    ) : (
                      <Badge variant={subAgent.is_public ? 'default' : 'secondary'} className="text-[10px]">
                        {subAgent.is_public ? 'Public' : 'Private'}
                      </Badge>
                    )}
                  </div>
                </ConfigSection>

                {/* Type-specific configuration */}
                {subAgent.type === 'remote' ? (
                  <>
                    {/* Section: Connection */}
                    <ConfigSection title="Connection" icon={Plug}>
                      <div className="space-y-1.5">
                        <Label htmlFor="agentUrl" className="text-xs">Agent URL</Label>
                        {isEditing ? (
                          <Input
                            id="agentUrl"
                            value={editAgentUrl}
                            onChange={(e) => {
                              setEditAgentUrl(e.target.value);
                              handleFieldChange();
                            }}
                            onFocus={() => setActiveFocusArea('config')}
                            onBlur={() => setActiveFocusArea(null)}
                            placeholder="https://..."
                            className="h-8 text-sm font-mono"
                          />
                        ) : (
                          <p className="text-sm font-mono break-all bg-muted p-2 rounded">{displayedAgentUrl}</p>
                        )}
                      </div>
                    </ConfigSection>

                    {/* Pricing */}
                    <PricingConfigurationSection
                      isEditing={isEditing}
                      expanded={pricingExpanded}
                      onExpandedChange={setPricingExpanded}
                      rateCardEntries={editRateCardEntries}
                      onRateCardEntriesChange={setEditRateCardEntries}
                      onFieldChange={handleFieldChange}
                      onFocusAreaChange={setActiveFocusArea}
                      pricingConfig={subAgent?.config_version?.pricing_config}
                    />
                  </>
                ) : subAgent.type === 'foundry' ? (
                  <>
                    {/* Section: Foundry Connection */}
                    <ConfigSection title="Foundry Connection" icon={Database}>
                      <div className="space-y-1.5">
                        <Label htmlFor="foundryHostname" className="text-xs">Hostname</Label>
                        {isEditing ? (
                          <Input
                            id="foundryHostname"
                            value={editFoundryHostname}
                            onChange={(e) => {
                              setEditFoundryHostname(e.target.value);
                              handleFieldChange();
                            }}
                            onFocus={() => setActiveFocusArea('config')}
                            onBlur={() => setActiveFocusArea(null)}
                            placeholder="example.palantirfoundry.com"
                            className="h-8 text-sm font-mono"
                          />
                        ) : (
                          <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                            {displayedFoundryHostname || 'Not configured'}
                          </p>
                        )}
                      </div>

                      <div className="space-y-1.5">
                        <Label htmlFor="foundryClientId" className="text-xs">Client ID</Label>
                        {isEditing ? (
                          <Input
                            id="foundryClientId"
                            value={editFoundryClientId}
                            onChange={(e) => {
                              setEditFoundryClientId(e.target.value);
                              handleFieldChange();
                            }}
                            onFocus={() => setActiveFocusArea('config')}
                            onBlur={() => setActiveFocusArea(null)}
                            placeholder="client-id"
                            className="h-8 text-sm font-mono"
                          />
                        ) : (
                          <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                            {displayedFoundryClientId || 'Not configured'}
                          </p>
                        )}
                      </div>

                      <div className="space-y-1.5">
                        <Label htmlFor="foundryClientSecretRef" className="text-xs flex items-center gap-1.5">
                          <Key className="h-3 w-3" />
                          Client Secret
                        </Label>
                        {isEditing ? (
                          <>
                            <Select
                              value={editFoundryClientSecretRef?.toString() ?? ''}
                              onValueChange={(value) => {
                                setEditFoundryClientSecretRef(value ? parseInt(value) : null);
                                handleFieldChange();
                              }}
                            >
                              <SelectTrigger
                                id="foundryClientSecretRef"
                                onFocus={() => setActiveFocusArea('config')}
                                onBlur={() => setActiveFocusArea(null)}
                                className="h-8 text-sm"
                              >
                                <SelectValue placeholder="Select a secret" />
                              </SelectTrigger>
                              <SelectContent>
                                {availableSecrets.length === 0 ? (
                                  <div className="p-3 text-center text-xs text-muted-foreground">
                                    No secrets available. Create one in Settings → Secrets Vault.
                                  </div>
                                ) : (
                                  availableSecrets.map((secret) => (
                                    <SelectItem key={secret.id} value={secret.id.toString()}>
                                      {secret.name}
                                    </SelectItem>
                                  ))
                                )}
                              </SelectContent>
                            </Select>
                            <p className="text-[11px] text-muted-foreground flex items-center gap-1">
                              <Lock className="h-2.5 w-2.5" />
                              Stored securely in AWS SSM Parameter Store
                            </p>
                          </>
                        ) : (
                          <p className="text-sm text-muted-foreground flex items-center gap-1.5">
                            <Lock className="h-3.5 w-3.5" />
                            {displayedFoundryClientSecretRef
                              ? availableSecrets.find((s) => s.id === displayedFoundryClientSecretRef)?.name ||
                                `Secret ID: ${displayedFoundryClientSecretRef}`
                              : 'Not configured'}
                          </p>
                        )}
                      </div>

                      <div className="space-y-1.5">
                        <Label htmlFor="foundryOntologyRid" className="text-xs">Ontology RID</Label>
                        {isEditing ? (
                          <Input
                            id="foundryOntologyRid"
                            value={editFoundryOntologyRid}
                            onChange={(e) => {
                              setEditFoundryOntologyRid(e.target.value);
                              handleFieldChange();
                            }}
                            onFocus={() => setActiveFocusArea('config')}
                            onBlur={() => setActiveFocusArea(null)}
                            placeholder="ri.ontology.main.ontology.xxx"
                            className="h-8 text-sm font-mono"
                          />
                        ) : (
                          <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                            {displayedFoundryOntologyRid || 'Not configured'}
                          </p>
                        )}
                      </div>

                      <div className="space-y-1.5">
                        <Label htmlFor="foundryQueryApiName" className="text-xs">Query API Name</Label>
                        {isEditing ? (
                          <Input
                            id="foundryQueryApiName"
                            value={editFoundryQueryApiName}
                            onChange={(e) => {
                              setEditFoundryQueryApiName(e.target.value);
                              handleFieldChange();
                            }}
                            onFocus={() => setActiveFocusArea('config')}
                            onBlur={() => setActiveFocusArea(null)}
                            placeholder="myQueryApi"
                            className="h-8 text-sm font-mono"
                          />
                        ) : (
                          <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                            {displayedFoundryQueryApiName || 'Not configured'}
                          </p>
                        )}
                      </div>

                      <div className="space-y-1.5">
                        <Label className="text-xs">API Scopes</Label>
                        {isEditing ? (
                          <div className="grid grid-cols-1 gap-1.5 p-2 border rounded text-xs">
                            {[
                              { value: 'api:use-ontologies-read', label: 'Ontologies Read' },
                              { value: 'api:use-ontologies-write', label: 'Ontologies Write' },
                              { value: 'api:use-aip-agents-read', label: 'AIP Agents Read' },
                              { value: 'api:use-aip-agents-write', label: 'AIP Agents Write' },
                              { value: 'api:use-mediasets-read', label: 'Mediasets Read' },
                              { value: 'api:use-mediasets-write', label: 'Mediasets Write' },
                            ].map((scope) => (
                              <label key={scope.value} className="flex items-center gap-2">
                                <input
                                  type="checkbox"
                                  checked={editFoundryScopes.includes(scope.value)}
                                  onChange={(e) => {
                                    if (e.target.checked) {
                                      setEditFoundryScopes([...editFoundryScopes, scope.value]);
                                    } else {
                                      setEditFoundryScopes(editFoundryScopes.filter((s) => s !== scope.value));
                                    }
                                    handleFieldChange();
                                  }}
                                  className="rounded border-gray-300"
                                />
                                <span>{scope.label}</span>
                              </label>
                            ))}
                          </div>
                        ) : (
                          <div className="space-y-1 text-sm bg-muted p-2 rounded">
                            {Array.isArray(displayedFoundryScopes) && displayedFoundryScopes.length > 0 ? (
                              displayedFoundryScopes.map((scope) => (
                                <div key={scope} className="flex items-center gap-2">
                                  <CheckCircle className="h-3 w-3 text-muted-foreground" />
                                  <code className="text-xs">{scope}</code>
                                </div>
                              ))
                            ) : (
                              <p className="text-xs text-muted-foreground">No scopes configured</p>
                            )}
                          </div>
                        )}
                      </div>

                      {(isEditing || displayedFoundryVersion) && (
                        <div className="space-y-1.5">
                          <Label htmlFor="foundryVersion" className="text-xs">Version (Optional)</Label>
                          {isEditing ? (
                            <Input
                              id="foundryVersion"
                              value={editFoundryVersion}
                              onChange={(e) => {
                                setEditFoundryVersion(e.target.value);
                                handleFieldChange();
                              }}
                              onFocus={() => setActiveFocusArea('config')}
                              onBlur={() => setActiveFocusArea(null)}
                              placeholder="v1"
                              className="h-8 text-sm"
                            />
                          ) : (
                            <p className="text-sm font-mono bg-muted p-2 rounded">{displayedFoundryVersion}</p>
                          )}
                        </div>
                      )}
                    </ConfigSection>

                    {/* Pricing */}
                    <PricingConfigurationSection
                      isEditing={isEditing}
                      expanded={pricingExpanded}
                      onExpandedChange={setPricingExpanded}
                      rateCardEntries={editRateCardEntries}
                      onRateCardEntriesChange={setEditRateCardEntries}
                      onFieldChange={handleFieldChange}
                      onFocusAreaChange={setActiveFocusArea}
                      pricingConfig={subAgent?.config_version?.pricing_config}
                    />
                  </>
                ) : (
                  <>
                    {/* Section: Model & Intelligence (local agents) */}
                    <ConfigSection title="Model" icon={Cpu}>
                      <div className="space-y-1.5">
                        {isEditing ? (
                          <Select
                            value={editModel}
                            onValueChange={(value) => {
                              setEditModel(value);
                              // Tier selections have no concrete alias to check capabilities against.
                              if (!value.startsWith('tier:') && !modelSupportsThinking(value, availableModels)) {
                                setEditEnableThinking(false);
                                setEditThinkingLevel(null);
                              }
                              handleFieldChange();
                            }}
                          >
                            <SelectTrigger id="model" className="h-8 text-sm">
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
                                {modelSelectOptions(editModelAlias, availableModels, displayedModelRetired).options.map((option) => (
                                  <SelectItem key={option.value} value={option.value}>
                                    {option.label}
                                  </SelectItem>
                                ))}
                              </SelectGroup>
                            </SelectContent>
                          </Select>
                        ) : displayedModelTier ? (
                          <span className="text-sm text-foreground">
                            {displayedModelTier} tier
                            {displayedEffectiveModel ? (
                              <span className="text-muted-foreground"> → {displayedEffectiveModel}</span>
                            ) : null}
                          </span>
                        ) : (
                          <ModelStatusText
                            value={displayedModel}
                            modelRetired={displayedModelRetired}
                            effectiveModel={displayedEffectiveModel}
                          />
                        )}
                        {isEditing && !editIsTier && modelSelectOptions(editModelAlias, availableModels, displayedModelRetired).retiredValue && (
                          <p className="text-[11px] text-amber-600 dark:text-amber-400">
                            This model was retired. Select a replacement to update the agent.
                          </p>
                        )}
                        {isEditing && editIsTier && (
                          <p className="text-[11px] text-muted-foreground">
                            Runs on the current default for this tier — survives model upgrades.
                          </p>
                        )}
                      </div>

                      {/* Extended Thinking — only offered for models the gateway reports as
                          thinking-capable, so we never let the user enable a config the
                          backend would silently drop on save. */}
                      {isEditing ? (
                        modelSupportsThinking(editModelAlias, availableModels) && (
                          <div className="flex items-center justify-between">
                            <div className="space-y-0.5">
                              <span className="text-xs font-medium text-foreground">Extended Thinking</span>
                              <p className="text-[11px] text-muted-foreground">Enable extended thinking for complex reasoning tasks</p>
                            </div>
                            <Switch
                              checked={editEnableThinking}
                              onCheckedChange={(checked) => {
                                setEditEnableThinking(checked);
                                if (!checked) {
                                  setEditThinkingLevel(null);
                                } else if (editThinkingLevel === null) {
                                  setEditThinkingLevel('low');
                                }
                                handleFieldChange();
                              }}
                            />
                          </div>
                        )
                      ) : (
                        displayedEnableThinking && (
                          <div className="flex items-center justify-between">
                            <span className="text-xs font-medium text-foreground">Extended Thinking</span>
                            <Badge variant="secondary" className="text-[10px]">
                              {displayedThinkingLevel ? displayedThinkingLevel.charAt(0).toUpperCase() + displayedThinkingLevel.slice(1) : 'On'}
                            </Badge>
                          </div>
                        )
                      )}

                      {isEditing && editEnableThinking && modelSupportsThinking(editModelAlias, availableModels) && (
                        <div className="space-y-1.5 pl-1">
                          <span className="text-[11px] text-muted-foreground">Thinking Level</span>
                          <Select
                            value={editThinkingLevel || undefined}
                            onValueChange={(value) => {
                              setEditThinkingLevel(value as OrchestratorThinkingLevel);
                              handleFieldChange();
                            }}
                          >
                            <SelectTrigger className="h-7 text-xs w-full max-w-[180px]">
                              <SelectValue placeholder="Select level" />
                            </SelectTrigger>
                            <SelectContent>
                              {getAvailableThinkingLevels(editModelAlias, availableModels).map((option) => (
                                <SelectItem key={option.value} value={option.value}>
                                  {option.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      )}
                    </ConfigSection>

                    {/* Section: Tools & Skills */}
                    <ConfigSection title="Tools & Skills" icon={Wrench}>
                      {/* MCP Tools */}
                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-medium text-foreground">MCP Tools</span>
                          {isEditing && (
                            <Button variant="outline" size="sm" className="h-6 text-[11px] px-2" onClick={() => setShowMcpToolsSheet(true)}>
                              <Wrench className="h-2.5 w-2.5 mr-1" />
                              {editMcpTools.length > 0 ? `${editMcpTools.length} selected` : 'Select'}
                            </Button>
                          )}
                        </div>
                        {!isEditing && (
                          Array.isArray(displayedMcpTools) && displayedMcpTools.length > 0 ? (
                            <Collapsible open={mcpToolsExpanded} onOpenChange={setMcpToolsExpanded}>
                              <CollapsibleTrigger asChild>
                                <button type="button" className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors">
                                  <ChevronDown className={`h-3 w-3 transition-transform ${mcpToolsExpanded ? '' : '-rotate-90'}`} />
                                  {displayedMcpTools.length} tools configured
                                </button>
                              </CollapsibleTrigger>
                              <CollapsibleContent className="pt-1.5">
                                <div className="space-y-0.5 bg-muted/50 p-2 rounded">
                                  {displayedMcpTools.map((tool) => (
                                    <div key={tool} className="flex items-center gap-1.5">
                                      <Wrench className="h-2.5 w-2.5 text-muted-foreground" />
                                      <code className="text-[11px]">{tool}</code>
                                    </div>
                                  ))}
                                </div>
                              </CollapsibleContent>
                            </Collapsible>
                          ) : (
                            <p className="text-[11px] text-muted-foreground">No MCP tools configured.</p>
                          )
                        )}
                      </div>

                      <hr className="border-border/40" />

                      {/* Skills */}
                      <div className="space-y-2 min-w-0 w-full">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-medium text-foreground">Skills</span>
                          {isEditing && (
                            <div className="flex gap-1">
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                className="h-6 text-[11px] px-2"
                                onClick={() => setIsSkillImportOpen(true)}
                              >
                                <Plus className="h-2.5 w-2.5 mr-1" />
                                Import
                              </Button>
                              {editSkills.some((s) => s.scope === 'sub-agent' || !s.registry_id) ? (
                                <Button
                                  type="button"
                                  variant="outline"
                                  size="sm"
                                  className="h-6 text-[11px] px-2"
                                  onClick={() => setIsSkillModalOpen(true)}
                                >
                                  <Pencil className="h-2.5 w-2.5 mr-1" />
                                  Edit Custom
                                </Button>
                              ) : (
                                <Button
                                  type="button"
                                  variant="outline"
                                  size="sm"
                                  className="h-6 text-[11px] px-2"
                                  onClick={() => setIsSkillModalOpen(true)}
                                >
                                  <Plus className="h-2.5 w-2.5 mr-1" />
                                  Create
                                </Button>
                              )}
                            </div>
                          )}
                        </div>
                        {(() => {
                          const skillsList = isEditing ? editSkills : displayedSkills;
                          return Array.isArray(skillsList) && skillsList.length > 0 ? (
                            <div className="space-y-1">
                              {skillsList.map((skill: SkillDefinition, idx: number) => (
                                <div
                                  key={skill.name}
                                  className="flex items-center gap-2 py-1 px-2 rounded bg-muted/40 text-[11px] group/skill"
                                >
                                  {skill.scope && skill.scope !== 'sub-agent' ? (
                                    <Tooltip>
                                      <TooltipTrigger asChild>
                                        <a
                                          href={`/app/skill-registry?skill=${skill.registry_id}`}
                                          className="font-mono font-medium shrink-0 whitespace-nowrap text-primary hover:underline inline-flex items-center gap-0.5"
                                        >
                                          {skill.name || '(unnamed)'}
                                          <ExternalLink className="h-2.5 w-2.5 opacity-60" />
                                        </a>
                                      </TooltipTrigger>
                                      <TooltipContent>View in skill registry</TooltipContent>
                                    </Tooltip>
                                  ) : (
                                    <code className="font-mono font-medium shrink-0 whitespace-nowrap">{skill.name || '(unnamed)'}</code>
                                  )}
                                  {skill.scope && skill.scope !== 'sub-agent' && (
                                    <span className="text-[10px] text-muted-foreground bg-muted px-1 rounded shrink-0">imported</span>
                                  )}
                                  {!isEditing && skill.scope && skill.scope !== 'sub-agent' && skill.update_available && skill.registry_id && skill.content_hash && (
                                    <button
                                      type="button"
                                      className="text-[10px] text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-950 px-1 rounded shrink-0 hover:bg-amber-100 dark:hover:bg-amber-900 cursor-pointer"
                                      onClick={() => setSkillDiffInfo({
                                        registryId: skill.registry_id!,
                                        contentHash: skill.content_hash!,
                                        name: skill.name || 'Skill',
                                        ...(canEdit && { updateTarget: { type: 'imported-skill-direct' as const, skillName: skill.name! } }),
                                      })}
                                    >
                                      update available
                                    </button>
                                  )}
                                  {isEditing && skill.scope && skill.scope !== 'sub-agent' && skill.name && skillsWithUpdates.has(skill.name) && skill.registry_id && skill.content_hash && (
                                    <button
                                      type="button"
                                      className="text-[10px] text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-950 px-1 rounded shrink-0 hover:bg-amber-100 dark:hover:bg-amber-900 cursor-pointer"
                                      disabled={updatingSkillName === skill.name}
                                      onClick={() => setSkillDiffInfo({
                                        registryId: skill.registry_id!,
                                        contentHash: skill.content_hash!,
                                        name: skill.name || 'Skill',
                                        updateTarget: { type: 'imported-skill', skillName: skill.name! },
                                      })}
                                    >
                                      {updatingSkillName === skill.name ? (
                                        <Loader2 className="h-3 w-3 animate-spin inline" />
                                      ) : (
                                        'update available'
                                      )}
                                    </button>
                                  )}
                                  {(skill.files?.length ?? 0) > 0 && (
                                    <span className="text-[10px] text-muted-foreground shrink-0">
                                      {skill.files!.length} files
                                    </span>
                                  )}
                                  {skill.description && (
                                    <span className="text-muted-foreground truncate flex-1">
                                      — {skill.description.length > 50 ? skill.description.slice(0, 50) + '…' : skill.description}
                                    </span>
                                  )}
                                  {isEditing && skill.scope && skill.scope !== 'sub-agent' && (
                                    <div className="flex items-center gap-1 opacity-0 group-hover/skill:opacity-100 transition-opacity ml-auto shrink-0">
                                      <button
                                        type="button"
                                        className="text-destructive hover:text-destructive/80"
                                        onClick={() => {
                                          setEditSkills((prev) => prev.filter((_, i) => i !== idx));
                                          handleFieldChange();
                                        }}
                                      >
                                        <Trash2 className="h-3 w-3" />
                                      </button>
                                    </div>
                                  )}
                                  {isEditing && (!skill.scope || skill.scope === 'sub-agent') && (
                                    <button
                                      type="button"
                                      className="opacity-0 group-hover/skill:opacity-100 text-destructive hover:text-destructive/80 transition-opacity ml-auto shrink-0"
                                      onClick={() => {
                                        setEditSkills((prev) => prev.filter((_, i) => i !== idx));
                                        handleFieldChange();
                                      }}
                                    >
                                      <Trash2 className="h-3 w-3" />
                                    </button>
                                  )}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <p className="text-[11px] text-muted-foreground">No skills defined.</p>
                          );
                        })()}
                      </div>

                      {/* Skill import from registry dialog */}
                      {isEditing && (
                        <SkillRegistryBrowseDialog
                          open={isSkillImportOpen}
                          onOpenChange={setIsSkillImportOpen}
                          title="Add skill from registry"
                          description="Search for a skill to import into this agent's configuration."
                          actionLabel="Import"
                          onAction={(skill) => handleImportSkillFromRegistry(skill)}
                          actionPending={!!importingSkillId}
                        />
                      )}

                      {/* Skill inline editor modal (for editing body/files - custom skills only) */}
                      {isEditing && (
                        <SkillEditorModal
                          open={isSkillModalOpen}
                          onOpenChange={setIsSkillModalOpen}
                          skills={editSkills.filter((s) => s.scope === 'sub-agent' || !s.registry_id) as SkillDefinition[]}
                          onChange={(updated) => {
                            const importedSkills = editSkills.filter((s) => s.scope && s.scope !== 'sub-agent');
                            // Build a lookup of existing sub-agent skills to preserve registry_id/scope
                            const existingByName = new Map(
                              editSkills
                                .filter((s) => s.scope === 'sub-agent' || !s.registry_id)
                                .map((s) => [s.name, s])
                            );
                            const customSkills = updated.map(s => {
                              const existing = existingByName.get(s.name);
                              return {
                                name: s.name,
                                description: s.description,
                                body: s.body ?? '',
                                files: s.files?.map((f: { path: string; content: string }) => ({ path: f.path, content: f.content })),
                                registry_id: existing?.registry_id ?? null,
                                scope: existing?.scope ?? null,
                              };
                            });
                            setEditSkills([...importedSkills, ...customSkills]);
                            handleFieldChange();
                          }}
                        />
                      )}

                      <hr className="border-border/40" />

                      {/* Sandbox Toggle */}
                      <div className="flex items-center justify-between">
                        <div className="space-y-0.5">
                          <span className="text-xs font-medium text-foreground">Sandbox Execution</span>
                          <p className="text-[11px] text-muted-foreground">
                            Run skill scripts in isolation
                          </p>
                        </div>
                        {isEditing ? (
                          <Switch
                            checked={editSandboxEnabled}
                            onCheckedChange={(checked) => {
                              setEditSandboxEnabled(checked);
                              if (!checked) setEditSandboxAutoEnabled(false);
                              handleFieldChange();
                            }}
                          />
                        ) : (
                          <Badge variant={displayedSandboxEnabled ? 'default' : 'secondary'} className="text-[10px]">
                            {displayedSandboxEnabled ? 'Enabled' : 'Disabled'}
                          </Badge>
                        )}
                      </div>
                      {isEditing && editSandboxAutoEnabled && (
                        <p className="text-[11px] text-amber-600 dark:text-amber-400">
                          Auto-enabled because one or more skills contain executable files (.py, .sh, etc.)
                        </p>
                      )}
                    </ConfigSection>

                    {/* Section: System Prompt */}
                    <ConfigSection title="System Prompt" icon={Code} defaultOpen={true}>
                      <div className="flex flex-col gap-2 min-h-0">
                        {isEditing ? (
                          <div className="flex flex-col gap-2 min-h-0">
                            {/* Edit/Preview Tabs */}
                            <div className="flex gap-1 p-0.5 bg-muted rounded-md">
                              <button
                                className={`flex-1 px-2 py-1 text-xs font-medium rounded transition-colors ${
                                  systemPromptTab === 'edit'
                                    ? 'bg-background text-foreground shadow-sm'
                                    : 'text-muted-foreground hover:text-foreground'
                                }`}
                                onClick={() => setSystemPromptTab('edit')}
                              >
                                <Code className="inline h-3 w-3 mr-1" />
                                Edit
                              </button>
                              <button
                                className={`flex-1 px-2 py-1 text-xs font-medium rounded transition-colors ${
                                  systemPromptTab === 'preview'
                                    ? 'bg-background text-foreground shadow-sm'
                                    : 'text-muted-foreground hover:text-foreground'
                                }`}
                                onClick={() => setSystemPromptTab('preview')}
                              >
                                <Eye className="inline h-3 w-3 mr-1" />
                                Preview
                              </button>
                            </div>

                            {systemPromptTab === 'edit' ? (
                              <Textarea
                                id="systemPrompt"
                                value={editSystemPrompt}
                                onChange={(e) => {
                                  setEditSystemPrompt(e.target.value);
                                  handleFieldChange();
                                }}
                                onFocus={() => setActiveFocusArea('config')}
                                onBlur={() => setActiveFocusArea(null)}
                                className="font-mono text-xs min-h-[200px] resize-none"
                                placeholder="Enter the system prompt..."
                              />
                            ) : (
                              <div className="bg-muted p-3 rounded min-h-[200px] overflow-auto border">
                                <Markdown className="text-sm">{editSystemPrompt || '*No content to preview*'}</Markdown>
                              </div>
                            )}
                          </div>
                        ) : (
                          <div className="bg-muted p-3 rounded min-h-[100px] max-h-[300px] overflow-auto border">
                            <Markdown className="text-sm">{displayedSystemPrompt}</Markdown>
                          </div>
                        )}
                      </div>
                    </ConfigSection>
                  </>
                )}

                {hasUnsavedChanges && (
                  <Alert className="flex-shrink-0">
                    <AlertDescription>
                      You have unsaved changes. Save to test with the updated configuration.
                    </AlertDescription>
                  </Alert>
                )}
              </div>
            </ScrollArea>
            </TabsContent>

            <TabsContent value="personalize" className="flex-1 min-h-0 flex flex-col mt-0 data-[state=inactive]:hidden">
            <ScrollArea className="flex-1 min-h-0">
              <div className="p-4 space-y-4">
                <div className="space-y-2">
                  <p className="text-sm text-muted-foreground">
                    Skills you activate here extend this agent for your conversations (personal) or for everyone in a group.
                  </p>
                  <p className="text-xs text-muted-foreground/70">
                    Resolution: personal skills override group skills, which override the agent&apos;s built-in skills (by name).
                  </p>
                  <a
                    href="/app/skill-registry"
                    className="inline-flex items-center gap-1.5 text-xs text-primary hover:underline"
                  >
                    <ExternalLink className="h-3 w-3" />
                    Browse skill registry
                  </a>
                </div>
                <div className="space-y-1.5">
              {myActivations.length > 0 ? (
                myActivations.map((activation) => (
                  <div
                    key={activation.id}
                    className="flex items-center gap-2 py-1.5 px-2 rounded bg-background/60 text-[11px] group/activation"
                  >
                    <Blocks className="h-3 w-3 text-muted-foreground shrink-0" />
                    <a
                      href={`/app/skill-registry?skill=${(activation as any).skill_slug || activation.skill_name}`}
                      className="font-mono font-medium shrink-0 whitespace-nowrap text-primary hover:underline"
                    >
                      {activation.skill_name}
                    </a>
                    <Badge variant="outline" className="text-[9px] px-1 py-0 gap-0.5 shrink-0">
                      {activation.scope === 'group' ? <Users className="h-2 w-2" /> : <Lock className="h-2 w-2" />}
                      {activation.scope === 'group' ? (activation.group_name ?? 'Group') : 'Personal'}
                    </Badge>
                    {activation.update_available && (
                      <button
                        type="button"
                        onClick={() => setSkillDiffInfo({
                          registryId: activation.registry_id,
                          contentHash: activation.content_hash,
                          name: activation.skill_name,
                          updateTarget: { type: 'activation', activationId: activation.id },
                        })}
                      >
                        <Badge variant="default" className="text-[9px] px-1 py-0 bg-amber-500 hover:bg-amber-600 shrink-0 cursor-pointer">
                          <ArrowUpCircle className="h-2 w-2 mr-0.5" />
                          update
                        </Badge>
                      </button>
                    )}
                    {activation.skill_description && (
                      <span className="text-muted-foreground truncate flex-1">
                        — {activation.skill_description.length > 40 ? activation.skill_description.slice(0, 40) + '…' : activation.skill_description}
                      </span>
                    )}
                    <div className="flex items-center gap-0.5 opacity-0 group-hover/activation:opacity-100 transition-opacity ml-auto shrink-0">
                      {activation.update_available && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <button
                              type="button"
                              className="text-primary hover:text-primary/80 p-0.5"
                              onClick={() => setSkillDiffInfo({
                                registryId: activation.registry_id,
                                contentHash: activation.content_hash,
                                name: activation.skill_name,
                                updateTarget: { type: 'activation', activationId: activation.id },
                              })}
                              disabled={updateActivationMutation.isPending}
                            >
                              <RefreshCw className="h-3 w-3" />
                            </button>
                          </TooltipTrigger>
                          <TooltipContent>Update to latest</TooltipContent>
                        </Tooltip>
                      )}
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            type="button"
                            className="text-destructive hover:text-destructive/80 p-0.5"
                            onClick={() => setDeactivatingActivation(activation)}
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Deactivate</TooltipContent>
                      </Tooltip>
                    </div>
                  </div>
                ))
              ) : (
                <p className="text-[11px] text-muted-foreground text-center py-6">
                  No skills activated. Click &quot;Add Skill&quot; to add from registry.
                </p>
              )}
                </div>
              </div>
            </ScrollArea>
            </TabsContent>
          </div>
          </Tabs>

          {/* Group Access Panel */}
          {(isOwner || (isAdministrator && adminMode)) && (
            <div className="flex flex-col rounded-lg border border-border bg-muted/30 overflow-hidden flex-shrink-0">
              <div className="flex items-center gap-2 px-4 py-3 border-b border-border shrink-0">
                <Users className="h-4 w-4 text-muted-foreground" />
                <h2 className="text-sm font-semibold">Group Access</h2>
              </div>
              <div className="p-4 space-y-3">
                <p className="text-sm text-muted-foreground">
                  Control which groups can access this sub-agent. Changes to group access do not create new versions.
                </p>
                <Button variant="outline" size="sm" className="w-full" onClick={() => setShowPermissionsDialog(true)}>
                  <Users className="mr-2 h-4 w-4" />
                  Manage Group Access
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* Middle Panel - Conversation List */}
        {showConversationList && (
          <div className="w-56 flex flex-col rounded-lg border border-border bg-muted/30 overflow-hidden flex-shrink-0">
            <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
              <h3 className="text-sm font-semibold">Conversations</h3>
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={handleNewConversation}>
                <Plus className="h-4 w-4" />
              </Button>
            </div>
            <ScrollArea className="flex-1 min-h-0">
              {conversations.length === 0 ? (
                <div className="flex flex-col items-center justify-center gap-3 py-12 px-4 text-center">
                  <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center">
                    <MessageSquare className="w-6 h-6 text-muted-foreground" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-foreground">No conversations</p>
                    <p className="text-xs text-muted-foreground">Click + to start a new chat</p>
                  </div>
                </div>
              ) : (
                <div className="p-2 space-y-0.5">
                  {conversations.map((conv) => (
                    <div
                      key={conv.id}
                      className={`group w-full text-left px-3 py-2.5 rounded-md transition-colors duration-150 hover:bg-accent/50 flex items-start gap-3 cursor-pointer ${
                        activeConversationId === conv.id
                          ? 'bg-accent text-accent-foreground'
                          : 'text-foreground/80 hover:text-foreground'
                      }`}
                      onClick={() => handleSelectConversation(conv.id)}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          handleSelectConversation(conv.id);
                        }
                      }}
                    >
                      <MessageSquare
                        className={`w-4 h-4 mt-0.5 shrink-0 ${
                          activeConversationId === conv.id ? 'text-primary' : 'text-muted-foreground'
                        }`}
                      />
                      <div className="flex-1 min-w-0 space-y-0.5">
                        <div className="flex items-center justify-between gap-2">
                          <span
                            className={`text-sm truncate ${
                              activeConversationId === conv.id ? 'font-medium' : 'font-normal'
                            }`}
                          >
                            {conv.title}
                          </span>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-5 w-5 opacity-0 group-hover:opacity-100 hover:opacity-100 transition-opacity shrink-0"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteConversation(conv.id);
                            }}
                          >
                            <Trash2 className="h-3 w-3" />
                          </Button>
                        </div>
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <span>
                            {conv.messages.length} msg{conv.messages.length !== 1 ? 's' : ''}
                          </span>
                          <span className="text-muted-foreground/60">{getVersionLabel(conv.configVersion)}</span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </ScrollArea>
          </div>
        )}

        {/* Right Panel - Chat */}
        <div className="flex-1 flex flex-col rounded-lg border border-border bg-muted/30 min-w-0 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
            <div className="flex items-center gap-2">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => setShowConversationList(!showConversationList)}
                  >
                    <MessageSquare className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{showConversationList ? 'Hide conversations' : 'Show conversations'}</TooltipContent>
              </Tooltip>
              <h2 className="text-sm font-semibold">{activeConversation ? activeConversation.title : 'Test Chat'}</h2>
              {activeConversation && (
                <Badge variant="outline" className="text-xs">
                  {getVersionLabel(activeConversation.configVersion)}
                  {activeConversation.configVersion !== currentVersion && (
                    <span className="ml-1 text-amber-600">(outdated)</span>
                  )}
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-1">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={handleNewConversation}
                  >
                    <Plus className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>New conversation</TooltipContent>
              </Tooltip>
              {/* Show version history button when collapsed */}
              {versionHistory.length > 0 && versionSidebarCollapsed && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => setVersionSidebarCollapsed(false)}
                    >
                      <PanelRightOpen className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Show version history</TooltipContent>
                </Tooltip>
              )}
            </div>
          </div>

          {/* Messages */}
          <ScrollArea className="flex-1 min-h-0">
            <div className="p-4">
              {messages.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-center">
                  <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center mb-3">
                    <FlaskConical className="h-6 w-6 text-muted-foreground" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-foreground">Start Testing</p>
                    <p className="text-xs text-muted-foreground max-w-xs">
                      Send a message to test your sub-agent configuration
                    </p>
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  {messages.map((message) => (
                    <div
                      key={message.id}
                      className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
                    >
                      <div
                        className={`max-w-[80%] rounded-2xl px-4 py-2.5 ${
                          message.role === 'user'
                            ? 'bg-primary text-primary-foreground rounded-br-md'
                            : 'bg-card border border-border rounded-bl-md'
                        }`}
                      >
                        <Markdown inverted={message.role === 'user'} className="text-sm">
                          {message.content}
                        </Markdown>
                        <p
                          className={`text-xs mt-1 ${
                            message.role === 'user' ? 'text-primary-foreground/70' : 'text-muted-foreground'
                          }`}
                        >
                          {message.timestamp.toLocaleTimeString()}
                        </p>
                      </div>
                    </div>
                  ))}
                  {isLoading && (
                    <div className="flex justify-start">
                      <div className="bg-card border border-border rounded-2xl rounded-bl-md px-4 py-2.5">
                        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                      </div>
                    </div>
                  )}
                  <div ref={messagesEndRef} />
                </div>
              )}
            </div>
          </ScrollArea>

          {/* Input */}
          <div className="p-3 border-t border-border shrink-0">
            {isImpersonating ? (
              <Alert variant="default" className="border-amber-500/50 bg-amber-500/10">
                <AlertTriangle className="h-4 w-4 text-amber-600" />
                <AlertDescription className="text-amber-600 text-xs">
                  Playground chat is unavailable while impersonating. Chat requires the user's access token.
                </AlertDescription>
              </Alert>
            ) : (
              <div className="flex gap-2">
                <Textarea
                  value={inputValue}
                  onChange={(e) => setInputValue(e.target.value)}
                  onKeyDown={handleKeyDown}
                  onFocus={() => setActiveFocusArea('chat')}
                  onBlur={() => setActiveFocusArea(null)}
                  placeholder="Type a message to test..."
                  className="min-h-[44px] max-h-32 resize-none bg-background"
                  rows={1}
                  disabled={isImpersonating}
                />
                <Button
                  onClick={handleSendMessage}
                  disabled={!inputValue.trim() || isLoading || isImpersonating}
                  className="shrink-0"
                >
                  <Send className="h-4 w-4" />
                </Button>
              </div>
            )}
          </div>
        </div>

        {/* Right Panel - Version History Sidebar */}
        {versionHistory.length > 0 && (
          <VersionSidebar
            subAgent={subAgent}
            versions={versionHistory}
            isOwner={isOwner}
            isAdmin={adminMode}
            hasWriteAccess={hasGroupWriteAccess}
            isCollapsed={versionSidebarCollapsed}
            onCollapsedChange={(collapsed) => {
              setVersionSidebarCollapsed(collapsed);
              // Track focus when version sidebar is expanded
              if (!collapsed) {
                setActiveFocusArea('version');
              } else {
                setActiveFocusArea(null);
              }
            }}
            onRefresh={invalidateSubAgentQuery}
            viewingVersion={viewingVersionNumber}
            onViewVersion={(version) => {
              setViewingVersionNumber(version);
              // Exit edit mode when switching versions
              if (version !== null) {
                setIsEditing(false);
                setHasUnsavedChanges(false);
              }
            }}
          />
        )}
      </div>

      {/* ARIA live region for accessibility announcements */}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {activeFocusArea === 'config' && !layoutLocked && 'Configuration panel expanded'}
        {activeFocusArea === 'chat' && !layoutLocked && 'Chat panel expanded'}
        {activeFocusArea === 'version' && !layoutLocked && 'Version history panel active'}
        {layoutLocked && 'Layout locked'}
      </div>

      {/* Delete Confirmation Dialog */}
      <Dialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Sub-Agent</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete "{subAgent.name}"? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowDeleteDialog(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleDelete} disabled={deleteMutation.isPending}>
              {deleteMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Approval Dialog */}
      {approvalAction && (
        <ApprovalDialog
          subAgent={subAgent}
          action={approvalAction}
          open={!!approvalAction}
          onOpenChange={(open) => !open && setApprovalAction(null)}
          onConfirm={handleApprovalAction}
        />
      )}

      {/* Permissions Dialog */}
      {(isOwner || (isAdministrator && adminMode)) && (
        <SubAgentPermissionsDialog
          subAgentId={subAgent.id}
          subAgentName={subAgent.name}
          open={showPermissionsDialog}
          onOpenChange={setShowPermissionsDialog}
        />
      )}

      {/* Submit for Approval Dialog */}
      <Dialog open={showSubmitDialog} onOpenChange={setShowSubmitDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Submit for Approval</DialogTitle>
            <DialogDescription>
              Describe the changes in this version. This helps reviewers understand what was modified.
            </DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <Label htmlFor="change-summary">Change Summary</Label>
            <Textarea
              id="change-summary"
              placeholder="e.g., Updated system prompt to improve response quality..."
              value={submitChangeSummary}
              onChange={(e) => setSubmitChangeSummary(e.target.value)}
              className="mt-2"
              rows={4}
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setShowSubmitDialog(false);
                setSubmitChangeSummary('');
              }}
            >
              Cancel
            </Button>
            <Button
              onClick={handleSubmitForApproval}
              disabled={submitMutation.isPending || !submitChangeSummary.trim()}
            >
              {submitMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Send className="h-4 w-4 mr-2" />
              )}
              Submit
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Change Summary Dialog (for Save action) */}
      <Dialog open={showChangeSummaryDialog} onOpenChange={setShowChangeSummaryDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Save Configuration Changes</DialogTitle>
            <DialogDescription>Describe what you changed (optional but recommended for tracking).</DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <Label htmlFor="save-change-summary">Change Summary</Label>
            <Textarea
              id="save-change-summary"
              placeholder="e.g., Improved system prompt clarity, adjusted model selection..."
              value={changeSummary}
              onChange={(e) => setChangeSummary(e.target.value)}
              className="mt-2"
              rows={3}
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                handleSaveWithSummary('');
              }}
            >
              Skip
            </Button>
            <Button onClick={() => handleSaveWithSummary(changeSummary)} disabled={updateMutation.isPending}>
              {updateMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Save className="h-4 w-4 mr-2" />
              )}
              Save with Summary
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* MCP Tools Configuration Dialog */}
      <Dialog open={showMcpToolsSheet} onOpenChange={setShowMcpToolsSheet}>
        <DialogContent className="!w-[98vw] !max-w-[1800px] max-h-[90vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>Configure MCP Tools</DialogTitle>
            <DialogDescription>
              Select which MCP tools this sub-agent can use. If none are selected, it will inherit all tools from the
              orchestrator.
            </DialogDescription>
          </DialogHeader>
          <div className="flex-1 overflow-y-auto">
            <MCPToolToggleList
              value={editMcpTools}
              onChange={(tools) => {
                setEditMcpTools(tools);
                handleFieldChange();
              }}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowMcpToolsSheet(false)}>
              Done
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Activate Skill Dialog */}
      <SkillRegistryBrowseDialog
        open={showActivateSkillDialog}
        onOpenChange={setShowActivateSkillDialog}
        title="Activate skill from registry"
        description="Search for a skill to activate on this agent for yourself or a group."
        actionLabel="Activate"
        actionPending={activateSkillMutation.isPending}
        onAction={(skill) => {
          if (!id || !skill.id) return;
          if (activateScope === 'group' && !activateGroupId) {
            toast.error('Please select a group');
            return;
          }
          activateSkillMutation.mutate({
            body: {
              registry_id: skill.id,
              sub_agent_id: parseInt(id, 10),
              scope: activateScope,
              group_id: activateScope === 'group' ? activateGroupId : undefined,
            },
          });
        }}
        headerContent={
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Scope:</span>
            <Select
              value={activateScope}
              onValueChange={(v) => {
                setActivateScope(v as 'personal' | 'group');
                if (v === 'personal') setActivateGroupId(null);
                else if (myGroups.length > 0) setActivateGroupId(myGroups[0].id);
              }}
            >
              <SelectTrigger className="w-28 h-7 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="personal">Personal</SelectItem>
                <SelectItem value="group">Group</SelectItem>
              </SelectContent>
            </Select>
            {activateScope === 'group' && (
              <Select
                value={activateGroupId ? String(activateGroupId) : ''}
                onValueChange={(v) => setActivateGroupId(Number(v))}
              >
                <SelectTrigger className="flex-1 h-7 text-xs">
                  <SelectValue placeholder="Select group" />
                </SelectTrigger>
                <SelectContent>
                  {myGroups.map((g) => (
                    <SelectItem key={g.id} value={String(g.id)}>
                      {g.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        }
      />

      {/* Deactivate Skill Confirmation */}
      {deactivatingActivation && (
        <Dialog open={!!deactivatingActivation} onOpenChange={() => setDeactivatingActivation(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Deactivate skill?</DialogTitle>
              <DialogDescription>
                This will remove <strong>{deactivatingActivation.skill_name}</strong> from this agent.
                The skill remains in the registry and can be re-activated later.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setDeactivatingActivation(null)}>Cancel</Button>
              <Button
                variant="destructive"
                onClick={() => {
                  deactivateSkillMutation.mutate({
                    path: { activation_id: deactivatingActivation.id },
                  });
                }}
                disabled={deactivateSkillMutation.isPending}
              >
                {deactivateSkillMutation.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />}
                Deactivate
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* Skill Diff Dialog */}
      <SkillDiffDialog
        open={!!skillDiffInfo}
        onOpenChange={(open) => { if (!open) setSkillDiffInfo(null); }}
        registryId={skillDiffInfo?.registryId ?? ''}
        pinnedContentHash={skillDiffInfo?.contentHash ?? ''}
        skillName={skillDiffInfo?.name ?? ''}
        onConfirmUpdate={skillDiffInfo?.updateTarget ? handleConfirmSkillDiffUpdate : undefined}
        confirmPending={isSkillDiffUpdatePending}
        confirmLabel="Update to latest"
      />
    </div>
  );
}
