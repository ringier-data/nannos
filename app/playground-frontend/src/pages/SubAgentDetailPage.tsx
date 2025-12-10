import { useState, useEffect, useRef } from 'react';
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
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Separator } from '@/components/ui/separator';
import { Switch } from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { ApprovalDialog } from '@/components/subagents/ApprovalDialog';
import { SubAgentPermissionsDialog } from '@/components/subagents/SubAgentPermissionsDialog';
import { VersionSidebar } from '@/components/subagents/VersionSidebar';
import { MCPToolToggleList } from '@/components/settings/MCPToolToggleList';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import { getErrorMessage } from '@/lib/utils';
import {
  getSubAgentApiV1SubAgentsSubAgentIdGetOptions,
  getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGetOptions,
  updateSubAgentApiV1SubAgentsSubAgentIdPatchMutation,
  deleteSubAgentApiV1SubAgentsSubAgentIdDeleteMutation,
  submitForApprovalApiV1SubAgentsSubAgentIdSubmitPostMutation,
  reviewVersionApiV1SubAgentsSubAgentIdVersionsVersionReviewPostMutation,
  listSecretsApiV1SecretsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { SubAgentConfigVersion } from '@/api/generated/types.gen';
import type { SubAgentStatus } from '@/components/subagents/types';
import { Markdown } from '@/components/ui/markdown';
import { usePlaygroundChat } from '@/hooks/usePlaygroundChat';

const statusConfig: Record<SubAgentStatus, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline'; icon: typeof Clock }> = {
  draft: { label: 'Draft', variant: 'secondary', icon: Edit },
  pending_approval: { label: 'Pending Approval', variant: 'outline', icon: Clock },
  approved: { label: 'Approved', variant: 'default', icon: CheckCircle },
  rejected: { label: 'Rejected', variant: 'destructive', icon: XCircle },
};

export function SubAgentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user, adminMode } = useAuth();
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

  // Foundry configuration state
  const [editFoundryHostname, setEditFoundryHostname] = useState('');
  const [editFoundryClientId, setEditFoundryClientId] = useState('');
  const [editFoundryClientSecret, setEditFoundryClientSecret] = useState('');
  const [editFoundryClientSecretRef, setEditFoundryClientSecretRef] = useState<number | null>(null);
  const [editFoundryOntologyRid, setEditFoundryOntologyRid] = useState('');
  const [editFoundryQueryApiName, setEditFoundryQueryApiName] = useState('');
  const [editFoundryScopes, setEditFoundryScopes] = useState<string[]>([]);
  const [editFoundryVersion, setEditFoundryVersion] = useState('');

  // Chat input state
  const [inputValue, setInputValue] = useState('');
  const [showConversationList, setShowConversationList] = useState(false);
  const [versionSidebarCollapsed, setVersionSidebarCollapsed] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Fetch sub-agent
  const { data: subAgent } = useQuery({
    ...getSubAgentApiV1SubAgentsSubAgentIdGetOptions({
      path: { sub_agent_id: parseInt(id || '0', 10) },
    }),
    enabled: !!id,
  });

  // Determine if this is a local agent type
  const isLocalAgentType = subAgent?.type === 'local';
  const isFoundryAgentType = subAgent?.type === 'foundry';

  // Fetch available secrets for Foundry configuration
  const { data: secretsData } = useQuery({
    ...listSecretsApiV1SecretsGetOptions(),
    enabled: isFoundryAgentType,
  });
  
  const availableSecrets = secretsData?.items?.filter(
    (secret) => secret.secret_type === 'foundry_client_secret'
  ) ?? [];

  // Fetch version history for all agent types
  const { data: versionHistoryData } = useQuery({
    ...getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGetOptions({
      path: { sub_agent_id: parseInt(id || '0', 10) },
    }),
    enabled: !!id,
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
        return typeof key === 'object' && key !== null && '_id' in key && key._id === 'getSubAgentApiV1SubAgentsSubAgentIdGet';
      },
    });
    // Also invalidate version history
    queryClient.invalidateQueries({
      predicate: (query) => {
        const key = query.queryKey[0];
        return typeof key === 'object' && key !== null && '_id' in key && key._id === 'getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGet';
      },
    });
  };

  // Mutations
  const updateMutation = useMutation({
    ...updateSubAgentApiV1SubAgentsSubAgentIdPatchMutation(),
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

  const currentUserId = user?.id ?? '';
  const isOwner = subAgent?.owner_user_id === currentUserId;
  const isLocalAgent = isLocalAgentType;
  const canApprove = adminMode;
  
  // Sub-agent overall status (from config_version)
  const subAgentStatus = (subAgent?.config_version?.status ?? 'draft') as SubAgentStatus;
  
  // Get the current version's status (may differ from sub-agent status)
  const currentVersionData = versionHistory.find((v: SubAgentConfigVersion) => v.version === currentVersion);
  const currentVersionStatus = (currentVersionData?.status ?? subAgentStatus) as SubAgentStatus;
  
  // For header status display, always use current version's status (not the viewed version)
  const status: SubAgentStatus = currentVersionStatus;
  
  // Owners can edit at any status - but only when viewing current version
  const canEdit = isOwner && !isViewingHistoricalVersion;
  const canDelete = isOwner || canApprove;
  // Can submit if owner and current version is draft
  const canSubmitForApproval = isOwner && currentVersionStatus === 'draft';
  
  // Get displayed data based on whether viewing historical version
  const displayedDescription = isViewingHistoricalVersion 
    ? (viewedVersion?.description ?? subAgent?.config_version?.description ?? '')
    : (subAgent?.config_version?.description ?? '');
  const displayedModel = isViewingHistoricalVersion
    ? (viewedVersion?.model ?? subAgent?.config_version?.model ?? '')
    : (subAgent?.config_version?.model ?? '');
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

  // Get active conversation
  const activeConversation = conversations.find((c) => c.id === activeConversationId);

  useEffect(() => {
    if (subAgent) {
      initEditState(subAgent);
    }
  }, [subAgent]);

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
    setEditModel(sa.config_version?.model || '');
    if (sa.type === 'remote') {
      setEditAgentUrl(String(sa.config_version?.agent_url ?? ''));
    } else if (sa.type === 'foundry') {
      setEditFoundryHostname(String(sa.config_version?.foundry_hostname ?? ''));
      setEditFoundryClientId(String(sa.config_version?.foundry_client_id ?? ''));
      setEditFoundryClientSecret(''); // Always empty for security
      setEditFoundryClientSecretRef(sa.config_version?.foundry_client_secret_ref ?? null);
      setEditFoundryOntologyRid(String(sa.config_version?.foundry_ontology_rid ?? ''));
      setEditFoundryQueryApiName(String(sa.config_version?.foundry_query_api_name ?? ''));
      setEditFoundryScopes(Array.isArray(sa.config_version?.foundry_scopes) ? sa.config_version.foundry_scopes : []);
      setEditFoundryVersion(String(sa.config_version?.foundry_version ?? ''));
    } else {
      setEditSystemPrompt(String(sa.config_version?.system_prompt ?? ''));
      setEditMcpTools(Array.isArray(sa.config_version?.mcp_tools) ? sa.config_version.mcp_tools : []);
    }
  };

  const handleSave = async () => {
    // Show change summary dialog instead of saving directly
    setShowChangeSummaryDialog(true);
  };
  
  const handleSaveWithSummary = async (summary: string) => {
    if (!subAgent || !id) return;

    let typeSpecificConfig = {};
    if (subAgent.type === 'remote') {
      typeSpecificConfig = { agent_url: editAgentUrl };
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
    } else {
      typeSpecificConfig = {
        system_prompt: editSystemPrompt,
        mcp_tools: editMcpTools.length > 0 ? editMcpTools : undefined,
      };
    }

    updateMutation.mutate({
      path: { sub_agent_id: parseInt(id, 10) },
      body: {
        name: editName,
        is_public: editIsPublic,
        description: editDescription,
        model: editModel || undefined,
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
            This sub-agent is live with {formatVersionLabel(liveVersionData, liveVersion)}, but you're viewing {formatVersionLabel(currentVersionData, currentVersion)} which is {status === 'draft' ? 'a draft' : status === 'pending_approval' ? 'pending approval' : status}.
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
        <div className={`flex flex-col gap-4 flex-shrink-0 ${
          configPanelWidth === 'compact' ? 'w-[320px]' :
          configPanelWidth === 'wide' ? 'w-[560px]' :
          'w-[400px]'
        }`}>
          {/* Configuration Panel */}
          <div 
          className={`flex flex-col rounded-lg border overflow-hidden flex-1 min-h-0 motion-safe:transition-all motion-safe:duration-300 motion-safe:ease-in-out ${
            isEditing 
              ? 'border-amber-500/50 bg-amber-50/30 dark:bg-amber-950/20' 
              : 'border-border bg-muted/30'
          }`}
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => navigate('/app/subagents')}>
                <ArrowLeft className="h-4 w-4" />
              </Button>
              <h2 className="text-sm font-semibold">Configuration</h2>
              {isEditing && (
                <Badge variant="outline" className="text-xs border-amber-500 text-amber-600 bg-amber-50 dark:bg-amber-950">
                  <Edit className="mr-1 h-3 w-3" />
                  Editing
                </Badge>
              )}
              {isViewingHistoricalVersion && (
                <Badge variant="outline" className="text-xs border-amber-500 text-amber-600">
                  {formatVersionLabel(viewedVersion, viewingVersionNumber)}
                </Badge>
              )}
            </div>
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
                        {layoutLocked ? (
                          <Lock className="h-4 w-4 text-amber-600" />
                        ) : (
                          <Unlock className="h-4 w-4" />
                        )}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      <p>{layoutLocked ? 'Layout locked - click to enable auto-resize' : 'Auto-resize enabled - click to lock'}</p>
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
                    <SelectItem value="compact">Compact (320px)</SelectItem>
                    <SelectItem value="medium">Medium (400px)</SelectItem>
                    <SelectItem value="wide">Wide (560px)</SelectItem>
                  </SelectContent>
                </Select>
              )}
              {canEdit && !isViewingHistoricalVersion && (
                <>
                  {isEditing ? (
                    <>
                      <Button 
                        variant="ghost" 
                        size="icon" 
                        onClick={handleCancelEdit} 
                        disabled={updateMutation.isPending}
                        className="h-7 w-7"
                        title="Cancel editing"
                      >
                        <X className="h-4 w-4" />
                      </Button>
                      <Button 
                        size="icon" 
                        onClick={handleSave} 
                        disabled={updateMutation.isPending}
                        className="h-7 w-7"
                        title="Save changes"
                      >
                        {updateMutation.isPending ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Save className="h-4 w-4" />
                        )}
                      </Button>
                    </>
                  ) : (
                    <Button 
                      variant="ghost" 
                      size="icon" 
                      onClick={() => {
                        setIsEditing(true);
                        setActiveFocusArea('config');
                        setShowConversationList(false);
                      }}
                      className="h-7 w-7"
                      title="Edit configuration"
                    >
                      <Edit className="h-4 w-4" />
                    </Button>
                  )}
                </>
              )}
            </div>
          </div>
          
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
            <div className="p-4 space-y-4">
              {/* Name */}
              <div className="space-y-2">
                <Label htmlFor="name">Name</Label>
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
                  />
                ) : (
                  <p className="text-sm">{subAgent.name}</p>
                )}
              </div>

              {/* Public Access */}
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
                  <Badge variant={subAgent.is_public ? "default" : "secondary"}>
                    {subAgent.is_public ? "Public" : "Private"}
                  </Badge>
                )}
              </div>

              {/* Description */}
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label htmlFor="description">Description</Label>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <HelpCircle className="h-3.5 w-3.5 text-muted-foreground cursor-help" />
                      </TooltipTrigger>
                      <TooltipContent className="max-w-xs">
                        <p>The orchestrator uses this description to route conversations to the appropriate sub-agent. Be clear and specific about what this agent handles.</p>
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
                    rows={4}
                    placeholder="Describe what this sub-agent does and when it should be used..."
                  />
                ) : (
                  <p className="text-sm text-muted-foreground">
                    {displayedDescription || 'No description'}
                  </p>
                )}
              </div>

              <Separator />

              {/* Type-specific configuration */}
              {subAgent.type === 'remote' ? (
                <div className="space-y-2">
                  <Label htmlFor="agentUrl">Agent URL</Label>
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
                    />
                  ) : (
                    <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                      {displayedAgentUrl}
                    </p>
                  )}
                </div>
              ) : subAgent.type === 'foundry' ? (
                <>
                  {/* Foundry Configuration */}
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="foundryHostname">Foundry Hostname</Label>
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
                        />
                      ) : (
                        <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                          {displayedFoundryHostname || 'Not configured'}
                        </p>
                      )}
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="foundryClientId">Client ID</Label>
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
                        />
                      ) : (
                        <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                          {displayedFoundryClientId || 'Not configured'}
                        </p>
                      )}
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="foundryClientSecretRef" className="flex items-center gap-2">
                        <Key className="h-4 w-4" />
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
                            disabled={false}
                          >
                            <SelectTrigger
                              id="foundryClientSecretRef"
                              onFocus={() => setActiveFocusArea('config')}
                              onBlur={() => setActiveFocusArea(null)}
                            >
                              <SelectValue placeholder="Select a secret from vault" />
                            </SelectTrigger>
                            <SelectContent>
                              {availableSecrets.length === 0 ? (
                                <div className="p-4 text-center text-sm text-muted-foreground">
                                  No secrets available. Create a Foundry Client Secret in Settings → Secrets Vault first.
                                </div>
                              ) : (
                                availableSecrets.map((secret) => (
                                  <SelectItem key={secret.id} value={secret.id.toString()}>
                                    {secret.name}
                                    {secret.description && (
                                      <span className="text-xs text-muted-foreground ml-2">
                                        - {secret.description}
                                      </span>
                                    )}
                                  </SelectItem>
                                ))
                              )}
                            </SelectContent>
                          </Select>
                          <p className="text-xs text-muted-foreground flex items-center gap-1">
                            <Lock className="h-3 w-3" />
                            Select a secret from the vault. Secrets are stored securely in AWS SSM Parameter Store.
                          </p>
                        </>
                      ) : (
                        <p className="text-sm text-muted-foreground flex items-center gap-2">
                          <Lock className="h-4 w-4" />
                          {displayedFoundryClientSecretRef 
                            ? (availableSecrets.find(s => s.id === displayedFoundryClientSecretRef)?.name || `Secret ID: ${displayedFoundryClientSecretRef}`)
                            : 'Not configured'}
                        </p>
                      )}
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="foundryOntologyRid">Ontology RID</Label>
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
                        />
                      ) : (
                        <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                          {displayedFoundryOntologyRid || 'Not configured'}
                        </p>
                      )}
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="foundryQueryApiName">Query API Name</Label>
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
                        />
                      ) : (
                        <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                          {displayedFoundryQueryApiName || 'Not configured'}
                        </p>
                      )}
                    </div>

                    <div className="space-y-2">
                      <Label>API Scopes</Label>
                      {isEditing ? (
                        <div className="grid grid-cols-1 gap-2 p-3 border rounded">
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
                                checked={editFoundryScopes.includes(scope.value)}
                                onChange={(e) => {
                                  if (e.target.checked) {
                                    setEditFoundryScopes([...editFoundryScopes, scope.value]);
                                  } else {
                                    setEditFoundryScopes(editFoundryScopes.filter(s => s !== scope.value));
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
                      <div className="space-y-2">
                        <Label htmlFor="foundryVersion">Foundry Version (Optional)</Label>
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
                          />
                        ) : (
                          <p className="text-sm font-mono break-all bg-muted p-2 rounded">
                            {displayedFoundryVersion}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <>
                  {/* Model - Only for local agents */}
                  <div className="space-y-2">
                    <Label htmlFor="model">Model</Label>
                    {isEditing ? (
                      <Select
                        value={editModel}
                        onValueChange={(value) => {
                          setEditModel(value);
                          handleFieldChange();
                        }}
                      >
                        <SelectTrigger id="model">
                          <SelectValue placeholder="Select a model" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="gpt4o">GPT-4o</SelectItem>
                          <SelectItem value="claude-sonnet-4.5">Claude Sonnet 4.5</SelectItem>
                        </SelectContent>
                      </Select>
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        {displayedModel || 'Default'}
                      </p>
                    )}
                  </div>

                  {/* MCP Tools */}
                  <div className="space-y-2">
                    <Label>MCP Tools (Optional)</Label>
                    {isEditing ? (
                      <Button
                        variant="outline"
                        className="w-full"
                        onClick={() => setShowMcpToolsSheet(true)}
                      >
                        <Wrench className="mr-2 h-4 w-4" />
                        Configure MCP Tools ({editMcpTools.length} selected)
                      </Button>
                    ) : (
                      <Collapsible open={mcpToolsExpanded} onOpenChange={setMcpToolsExpanded}>
                        <CollapsibleTrigger asChild>
                          <Button variant="ghost" className="w-full justify-between p-2 h-auto">
                            <span className="text-sm">
                              {Array.isArray(displayedMcpTools) && displayedMcpTools.length > 0
                                ? `${displayedMcpTools.length} tools configured`
                                : 'Inherits orchestrator tools'}
                            </span>
                            <ChevronDown className={`h-4 w-4 transition-transform ${mcpToolsExpanded ? 'rotate-180' : ''}`} />
                          </Button>
                        </CollapsibleTrigger>
                        <CollapsibleContent className="pt-2">
                          {Array.isArray(displayedMcpTools) && displayedMcpTools.length > 0 ? (
                            <div className="space-y-1 text-sm bg-muted p-2 rounded">
                              {displayedMcpTools.map((tool) => (
                                <div key={tool} className="flex items-center gap-2">
                                  <Wrench className="h-3 w-3 text-muted-foreground" />
                                  <code className="text-xs">{tool}</code>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <p className="text-xs text-muted-foreground">
                              This sub-agent will use all MCP tools available to the orchestrator.
                            </p>
                          )}
                        </CollapsibleContent>
                      </Collapsible>
                    )}
                  </div>

                  {/* System Prompt */}
                  <div className="space-y-2">
                    <Label htmlFor="systemPrompt">System Prompt</Label>
                    {isEditing ? (
                      <div className="space-y-2">
                        {/* Edit/Preview Tabs */}
                        <div className="flex gap-1 p-1 bg-muted rounded-md">
                          <button
                            className={`flex-1 px-3 py-1.5 text-sm font-medium rounded transition-colors ${
                              systemPromptTab === 'edit'
                                ? 'bg-background text-foreground shadow-sm'
                                : 'text-muted-foreground hover:text-foreground'
                            }`}
                            onClick={() => setSystemPromptTab('edit')}
                          >
                            <Code className="inline h-3.5 w-3.5 mr-1" />
                            Edit
                          </button>
                          <button
                            className={`flex-1 px-3 py-1.5 text-sm font-medium rounded transition-colors ${
                              systemPromptTab === 'preview'
                                ? 'bg-background text-foreground shadow-sm'
                                : 'text-muted-foreground hover:text-foreground'
                            }`}
                            onClick={() => setSystemPromptTab('preview')}
                          >
                            <Eye className="inline h-3.5 w-3.5 mr-1" />
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
                            rows={12}
                            className="font-mono text-sm min-h-[200px]"
                            placeholder="Enter the system prompt for this sub-agent..."
                          />
                        ) : (
                          <div className="bg-muted p-3 rounded min-h-[200px] max-h-[400px] overflow-auto border">
                            <Markdown className="text-sm">
                              {editSystemPrompt || '*No content to preview*'}
                            </Markdown>
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="bg-muted p-3 rounded max-h-48 overflow-auto">
                        <Markdown className="text-sm">
                          {displayedSystemPrompt}
                        </Markdown>
                      </div>
                    )}
                  </div>
                </>
              )}

              {hasUnsavedChanges && (
                <Alert>
                  <AlertDescription>
                    You have unsaved changes. Save to test with the updated configuration.
                  </AlertDescription>
                </Alert>
              )}
            </div>
          </ScrollArea>
        </div>

        {/* Group Access Panel */}
        {isOwner && (
          <div className="flex flex-col rounded-lg border border-border bg-muted/30 overflow-hidden flex-shrink-0">
            <div className="flex items-center gap-2 px-4 py-3 border-b border-border shrink-0">
              <Users className="h-4 w-4 text-muted-foreground" />
              <h2 className="text-sm font-semibold">Group Access</h2>
            </div>
            <div className="p-4 space-y-3">
              <p className="text-sm text-muted-foreground">
                Control which groups can access this sub-agent. Changes to group access do not create new versions.
              </p>
              <Button
                variant="outline"
                size="sm"
                className="w-full"
                onClick={() => setShowPermissionsDialog(true)}
              >
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
                    <button
                      key={conv.id}
                      className={`w-full text-left px-3 py-2.5 rounded-md transition-colors duration-150 hover:bg-accent/50 flex items-start gap-3 ${
                        activeConversationId === conv.id
                          ? 'bg-accent text-accent-foreground'
                          : 'text-foreground/80 hover:text-foreground'
                      }`}
                      onClick={() => handleSelectConversation(conv.id)}
                    >
                      <MessageSquare className={`w-4 h-4 mt-0.5 shrink-0 ${
                        activeConversationId === conv.id ? 'text-primary' : 'text-muted-foreground'
                      }`} />
                      <div className="flex-1 min-w-0 space-y-0.5">
                        <div className="flex items-center justify-between gap-2">
                          <span className={`text-sm truncate ${
                            activeConversationId === conv.id ? 'font-medium' : 'font-normal'
                          }`}>
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
                          <span>{conv.messages.length} msg{conv.messages.length !== 1 ? 's' : ''}</span>
                          <span className="text-muted-foreground/60">{getVersionLabel(conv.configVersion)}</span>
                        </div>
                      </div>
                    </button>
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
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => setShowConversationList(!showConversationList)}
                title={showConversationList ? 'Hide conversations' : 'Show conversations'}
              >
                <MessageSquare className="h-4 w-4" />
              </Button>
              <h2 className="text-sm font-semibold">
                {activeConversation ? activeConversation.title : 'Test Chat'}
              </h2>
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
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={handleNewConversation} title="New conversation">
                <Plus className="h-4 w-4" />
              </Button>
              {/* Show version history button when collapsed */}
              {versionHistory.length > 0 && versionSidebarCollapsed && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => setVersionSidebarCollapsed(false)}
                  title="Show version history"
                >
                  <PanelRightOpen className="h-4 w-4" />
                </Button>
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
                        <Markdown 
                          inverted={message.role === 'user'}
                          className="text-sm"
                        >
                          {message.content}
                        </Markdown>
                        <p className={`text-xs mt-1 ${
                          message.role === 'user' ? 'text-primary-foreground/70' : 'text-muted-foreground'
                        }`}>
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
              />
              <Button onClick={handleSendMessage} disabled={!inputValue.trim() || isLoading} className="shrink-0">
                <Send className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>

        {/* Right Panel - Version History Sidebar */}
        {versionHistory.length > 0 && (
          <VersionSidebar
            subAgent={subAgent}
            versions={versionHistory}
            isOwner={isOwner}
            isAdmin={adminMode}
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
            <Button variant="outline" onClick={() => setShowDeleteDialog(false)}>Cancel</Button>
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
      {isOwner && (
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
            <Button variant="outline" onClick={() => {
              setShowSubmitDialog(false);
              setSubmitChangeSummary('');
            }}>
              Cancel
            </Button>
            <Button 
              onClick={handleSubmitForApproval} 
              disabled={submitMutation.isPending || !submitChangeSummary.trim()}
            >
              {submitMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Send className="h-4 w-4 mr-2" />}
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
            <DialogDescription>
              Describe what you changed (optional but recommended for tracking).
            </DialogDescription>
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
            <Button 
              onClick={() => handleSaveWithSummary(changeSummary)} 
              disabled={updateMutation.isPending}
            >
              {updateMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Save className="h-4 w-4 mr-2" />}
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
              Select which MCP tools this sub-agent can use. If none are selected, it will inherit all tools from the orchestrator.
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
    </div>
  );
}
