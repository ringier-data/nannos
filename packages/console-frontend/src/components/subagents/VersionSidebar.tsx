import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  CheckCircle,
  Clock,
  FileText,
  GitCompare,
  Loader2,
  MoreVertical,
  PanelRightClose,
  RotateCcw,
  Star,
  Trash2,
  XCircle,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Separator } from '@/components/ui/separator';
import {
  deleteVersionApiV1SubAgentsSubAgentIdVersionsVersionDelete,
  revertToVersionApiV1SubAgentsSubAgentIdVersionsVersionRevertPost,
  setDefaultVersionApiV1SubAgentsSubAgentIdDefaultVersionPut,
  submitVersionForApprovalApiV1SubAgentsSubAgentIdVersionsVersionSubmitPost,
} from '@/api/generated/sdk.gen';
import { VersionDiffViewer } from './VersionDiffViewer';
import type { SubAgent, SubAgentConfigVersion, SubAgentStatus } from './types';

const statusConfig: Record<
  SubAgentStatus,
  { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline'; icon: React.ElementType }
> = {
  draft: { label: 'Draft', variant: 'secondary', icon: FileText },
  pending_approval: { label: 'Pending', variant: 'outline', icon: Clock },
  approved: { label: 'Approved', variant: 'default', icon: CheckCircle },
  rejected: { label: 'Rejected', variant: 'destructive', icon: XCircle },
};

interface VersionSidebarProps {
  subAgent: SubAgent;
  versions: SubAgentConfigVersion[];
  isOwner: boolean;
  isAdmin: boolean;
  isCollapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  onRefresh?: () => void;
  viewingVersion?: number | null;
  onViewVersion?: (version: number | null) => void;
}

export function VersionSidebar({
  subAgent,
  versions,
  isOwner,
  isAdmin: _isAdmin,
  isCollapsed = false,
  onCollapsedChange,
  onRefresh,
  viewingVersion,
  onViewVersion,
}: VersionSidebarProps) {
  const queryClient = useQueryClient();
  const [diffDialogOpen, setDiffDialogOpen] = useState(false);
  const [compareVersions, setCompareVersions] = useState<{
    from: SubAgentConfigVersion | null;
    to: SubAgentConfigVersion | null;
  }>({ from: null, to: null });
  
  // Submit dialog state
  const [showSubmitDialog, setShowSubmitDialog] = useState(false);
  const [submitVersion, setSubmitVersion] = useState<number | null>(null);
  const [submitChangeSummary, setSubmitChangeSummary] = useState('');
  
  // Delete confirmation state
  const [deleteVersion, setDeleteVersion] = useState<number | null>(null);

  const defaultVersion = subAgent.default_version;
  const currentVersion = subAgent.current_version ?? versions.length;

  const setDefaultMutation = useMutation({
    mutationFn: (version: number) =>
      setDefaultVersionApiV1SubAgentsSubAgentIdDefaultVersionPut({
        path: { sub_agent_id: subAgent.id },
        body: { version },
      }),
    onSuccess: () => {
      toast.success('Default version updated');
      queryClient.invalidateQueries({ queryKey: ['subAgents'] });
      onRefresh?.();
    },
    onError: (error) => {
      toast.error('Failed to update default version', {
        description: error instanceof Error ? error.message : 'Unknown error',
      });
    },
  });

  const submitMutation = useMutation({
    mutationFn: ({ version, changeSummary }: { version: number; changeSummary: string }) =>
      submitVersionForApprovalApiV1SubAgentsSubAgentIdVersionsVersionSubmitPost({
        path: { sub_agent_id: subAgent.id, version },
        body: { change_summary: changeSummary },
      }),
    onSuccess: () => {
      toast.success('Version submitted for approval');
      queryClient.invalidateQueries({ queryKey: ['subAgents'] });
      setShowSubmitDialog(false);
      setSubmitVersion(null);
      setSubmitChangeSummary('');
      onRefresh?.();
    },
    onError: (error) => {
      toast.error('Failed to submit version', {
        description: error instanceof Error ? error.message : 'Unknown error',
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (version: number) =>
      deleteVersionApiV1SubAgentsSubAgentIdVersionsVersionDelete({
        path: { sub_agent_id: subAgent.id, version },
      }),
    onSuccess: (_, version) => {
      toast.success(`Version ${version} deleted`);
      queryClient.invalidateQueries({ queryKey: ['subAgents'] });
      queryClient.invalidateQueries({ 
        predicate: (query) => {
          const key = query.queryKey[0];
          return typeof key === 'object' && key !== null && '_id' in key && 
            (key._id === 'getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGet' ||
             key._id === 'getSubAgentApiV1SubAgentsSubAgentIdGet');
        },
      });
      setDeleteVersion(null);
      onRefresh?.();
    },
    onError: (error) => {
      toast.error('Failed to delete version', {
        description: error instanceof Error ? error.message : 'Unknown error',
      });
    },
  });

  const revertMutation = useMutation({
    mutationFn: (version: number) =>
      revertToVersionApiV1SubAgentsSubAgentIdVersionsVersionRevertPost({
        path: { sub_agent_id: subAgent.id, version },
      }),
    onSuccess: (_, version) => {
      toast.success(`Created new draft from version ${version}`, {
        description: 'Submit for approval to make it the default version.',
      });
      // Invalidate both subAgents list and versions queries
      queryClient.invalidateQueries({ queryKey: ['subAgents'] });
      queryClient.invalidateQueries({ 
        predicate: (query) => {
          const key = query.queryKey[0];
          return typeof key === 'object' && key !== null && '_id' in key && 
            (key._id === 'getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGet' ||
             key._id === 'getSubAgentApiV1SubAgentsSubAgentIdGet');
        },
      });
      onRefresh?.();
    },
    onError: (error) => {
      toast.error('Failed to revert to version', {
        description: error instanceof Error ? error.message : 'Unknown error',
      });
    },
  });

  const handleCompareWithDefault = (version: SubAgentConfigVersion) => {
    const defaultVer = versions.find((v) => v.version === defaultVersion);
    setCompareVersions({ from: defaultVer ?? null, to: version });
    setDiffDialogOpen(true);
  };

  const handleCompareWithPrevious = (version: SubAgentConfigVersion) => {
    // Find the actual previous version in the list (not just version - 1)
    // This handles cases where intermediate versions have been deleted
    const sortedVersions = [...versions].sort((a, b) => a.version - b.version);
    const currentIndex = sortedVersions.findIndex((v) => v.version === version.version);
    const prevVersion = currentIndex > 0 ? sortedVersions[currentIndex - 1] : null;
    
    if (!prevVersion) {
      toast.error('No previous version available', {
        description: 'The previous version may have been deleted.',
      });
      return;
    }
    
    setCompareVersions({ from: prevVersion, to: version });
    setDiffDialogOpen(true);
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  if (versions.length === 0) {
    return null;
  }

  // Collapsed view - just show a toggle button
  if (isCollapsed) {
    return (
      <div className="h-full flex flex-col bg-muted/30 border-l border-border w-0 overflow-hidden transition-all duration-200">
        {/* Empty when collapsed - toggle handled by parent */}
      </div>
    );
  }

  return (
    <>
      <div className="w-72 h-full flex flex-col bg-muted/30 border-l border-border transition-all duration-200 overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-foreground">Version History</h3>
            <Badge variant="secondary" className="text-xs">
              {versions.length}
            </Badge>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => onCollapsedChange?.(true)}
            aria-label="Hide version history"
          >
            <PanelRightClose className="h-4 w-4" />
          </Button>
        </div>

        <ScrollArea className="flex-1 min-h-0">
          <div className="p-3 space-y-2">
            {versions
              .map((version, _index, allVersions) => {
                const status = version.status ?? 'draft';
                const statusInfo = statusConfig[status];
                const StatusIcon = statusInfo.icon;
                const isDefault = version.version === defaultVersion;
                const isCurrent = version.version === currentVersion;
                const isViewing = viewingVersion === version.version || (viewingVersion === null && isCurrent);
                const isApproved = status === 'approved';
                const isDraft = status === 'draft';
                const isRejected = status === 'rejected';
                const canSetDefault = isOwner && isApproved && !isDefault;
                const canSubmit = isOwner && (isDraft || isRejected);
                const canRevert = isOwner && !isCurrent;
                const canCompare = version.version > 1 || (defaultVersion !== undefined && defaultVersion !== version.version);
                // Check if there's actually a previous version available in the list
                const sortedVersions = [...allVersions].sort((a, b) => a.version - b.version);
                const currentVersionIndex = sortedVersions.findIndex((v) => v.version === version.version);
                const hasPreviousVersion = currentVersionIndex > 0;
                // Can delete non-approved versions (except if it's the only version)
                const canDelete = isOwner && !isApproved && versions.length > 1;

                return (
                  <div
                    key={version.version}
                    className={`relative rounded-lg border p-3 transition-colors cursor-pointer ${
                      isViewing
                        ? 'border-blue-500 bg-blue-500/5 ring-1 ring-blue-500/20'
                        : isDefault
                          ? 'border-primary/50 bg-primary/5 hover:bg-primary/10'
                          : 'hover:bg-muted/50'
                    }`}
                    onClick={() => onViewVersion?.(isCurrent ? null : version.version)}
                  >
                    {/* Version identifier and current indicator */}
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        {/* Show release number for approved, hash for others */}
                        {isApproved && version.release_number ? (
                          <div
                            className={`flex h-7 min-w-7 px-2 items-center justify-center rounded-full text-sm font-medium ${
                              isViewing
                                ? 'bg-blue-500 text-white'
                                : isDefault
                                  ? 'bg-primary text-primary-foreground'
                                  : 'bg-muted text-muted-foreground'
                            }`}
                            title={`Release ${version.release_number}`}
                          >
                            v{version.release_number}
                          </div>
                        ) : (
                          <div
                            className={`flex h-7 px-2 items-center justify-center rounded text-xs font-mono ${
                              isViewing
                                ? 'bg-blue-500 text-white'
                                : 'bg-muted text-muted-foreground'
                            }`}
                            title={version.version_hash || `Version ${version.version}`}
                          >
                            {version.version_hash ? `#${version.version_hash.slice(0, 7)}` : `v${version.version}`}
                          </div>
                        )}
                        {isCurrent && (
                          <Badge variant="default" className="bg-blue-500 text-xs">
                            Current
                          </Badge>
                        )}
                        {isViewing && !isCurrent && (
                          <Badge variant="outline" className="text-xs border-blue-500 text-blue-500">
                            Viewing
                          </Badge>
                        )}
                        {isDefault && !isCurrent && (
                          <Badge variant="outline" className="gap-1 text-xs">
                            <Star className="h-3 w-3 fill-current" />
                            Default
                          </Badge>
                        )}
                      </div>

                      <DropdownMenu>
                        <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                          <Button variant="ghost" size="icon" className="h-7 w-7">
                            <MoreVertical className="h-4 w-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          {canCompare && defaultVersion && version.version !== defaultVersion && (
                            <DropdownMenuItem onClick={() => handleCompareWithDefault(version)}>
                              <GitCompare className="mr-2 h-4 w-4" />
                              Compare with default
                            </DropdownMenuItem>
                          )}
                          {hasPreviousVersion && (
                            <DropdownMenuItem onClick={() => handleCompareWithPrevious(version)}>
                              <GitCompare className="mr-2 h-4 w-4" />
                              Compare with previous
                            </DropdownMenuItem>
                          )}
                          {canSetDefault && (
                            <>
                              <Separator className="my-1" />
                              <DropdownMenuItem
                                onClick={() => setDefaultMutation.mutate(version.version)}
                                disabled={setDefaultMutation.isPending}
                              >
                                {setDefaultMutation.isPending ? (
                                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                ) : (
                                  <Star className="mr-2 h-4 w-4" />
                                )}
                                Set as default
                              </DropdownMenuItem>
                            </>
                          )}
                          {canSubmit && (
                            <>
                              <Separator className="my-1" />
                              <DropdownMenuItem
                                onClick={() => {
                                  setSubmitVersion(version.version);
                                  setShowSubmitDialog(true);
                                }}
                                disabled={submitMutation.isPending}
                              >
                                <Clock className="mr-2 h-4 w-4" />
                                Submit for approval
                              </DropdownMenuItem>
                            </>
                          )}
                          {canDelete && (
                            <>
                              <Separator className="my-1" />
                              <DropdownMenuItem
                                onClick={() => setDeleteVersion(version.version)}
                                disabled={deleteMutation.isPending}
                                className="text-destructive focus:text-destructive"
                              >
                                <Trash2 className="mr-2 h-4 w-4" />
                                Delete version
                              </DropdownMenuItem>
                            </>
                          )}
                          {canRevert && (
                            <>
                              <Separator className="my-1" />
                              <DropdownMenuItem
                                onClick={() => revertMutation.mutate(version.version)}
                                disabled={revertMutation.isPending}
                              >
                                {revertMutation.isPending ? (
                                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                ) : (
                                  <RotateCcw className="mr-2 h-4 w-4" />
                                )}
                                Create draft from this version
                              </DropdownMenuItem>
                            </>
                          )}
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>

                    {/* Status badge */}
                    <div className="flex items-center gap-2 mb-2">
                      <Badge variant={statusInfo.variant} className="gap-1 text-xs">
                        <StatusIcon className="h-3 w-3" />
                        {statusInfo.label}
                      </Badge>
                      {isDefault && isCurrent && (
                        <Badge variant="outline" className="gap-1 text-xs">
                          <Star className="h-3 w-3 fill-current" />
                          Default
                        </Badge>
                      )}
                    </div>

                    {/* Change summary */}
                    {version.change_summary && (
                      <p className="text-xs text-muted-foreground line-clamp-2 mb-1">
                        {version.change_summary}
                      </p>
                    )}

                    {/* Date */}
                    <p className="text-xs text-muted-foreground">
                      {formatDate(version.created_at)}
                    </p>

                    {/* Rejection reason */}
                    {status === 'rejected' && version.rejection_reason && (
                      <div className="mt-2 rounded-md bg-destructive/10 px-2 py-1 text-xs text-destructive">
                        <strong>Rejected:</strong> {version.rejection_reason}
                      </div>
                    )}
                  </div>
                );
              })}
          </div>
        </ScrollArea>
      </div>

      <VersionDiffViewer
        open={diffDialogOpen}
        onOpenChange={setDiffDialogOpen}
        fromVersion={compareVersions.from}
        toVersion={compareVersions.to}
        subAgentName={subAgent.name}
      />

      {/* Submit for Approval Dialog */}
      <Dialog open={showSubmitDialog} onOpenChange={setShowSubmitDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Submit Version {submitVersion} for Approval</DialogTitle>
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
              setSubmitVersion(null);
              setSubmitChangeSummary('');
            }}>
              Cancel
            </Button>
            <Button 
              onClick={() => {
                if (submitVersion !== null) {
                  submitMutation.mutate({ version: submitVersion, changeSummary: submitChangeSummary });
                }
              }} 
              disabled={submitMutation.isPending || !submitChangeSummary.trim()}
            >
              {submitMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Clock className="h-4 w-4 mr-2" />}
              Submit
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Version Confirmation Dialog */}
      <Dialog open={deleteVersion !== null} onOpenChange={(open) => !open && setDeleteVersion(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Version {deleteVersion}?</DialogTitle>
            <DialogDescription>
              This will permanently delete this version. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteVersion(null)}>
              Cancel
            </Button>
            <Button 
              variant="destructive"
              onClick={() => {
                if (deleteVersion !== null) {
                  deleteMutation.mutate(deleteVersion);
                }
              }} 
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Trash2 className="h-4 w-4 mr-2" />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
