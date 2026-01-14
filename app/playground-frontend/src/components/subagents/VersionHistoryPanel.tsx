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

const statusConfig: Record<SubAgentStatus, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline'; icon: React.ElementType }> = {
  draft: { label: 'Draft', variant: 'secondary', icon: FileText },
  pending_approval: { label: 'Pending', variant: 'outline', icon: Clock },
  approved: { label: 'Approved', variant: 'default', icon: CheckCircle },
  rejected: { label: 'Rejected', variant: 'destructive', icon: XCircle },
};

interface VersionHistoryPanelProps {
  subAgent: SubAgent;
  versions: SubAgentConfigVersion[];
  isOwner: boolean;
  isAdmin: boolean;
}

export function VersionHistoryPanel({ subAgent, versions, isOwner, isAdmin: _isAdmin }: VersionHistoryPanelProps) {
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
  const [deleteVersionNum, setDeleteVersionNum] = useState<number | null>(null);

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
      setDeleteVersionNum(null);
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
    onSuccess: () => {
      toast.success('Reverted to version - a new draft version has been created');
      queryClient.invalidateQueries({ queryKey: ['subAgents'] });
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
    const prevVersion = versions.find((v) => v.version === version.version - 1);
    setCompareVersions({ from: prevVersion ?? null, to: version });
    setDiffDialogOpen(true);
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  if (versions.length === 0) {
    return (
      <div className="text-center py-8 text-muted-foreground">
        No version history available
      </div>
    );
  }

  return (
    <>
      <ScrollArea className="h-[400px]">
        <div className="space-y-2 pr-4">
          {versions
            .slice()
            .reverse()
            .map((version) => {
              const status = version.status ?? 'draft';
              const statusInfo = statusConfig[status];
              const StatusIcon = statusInfo.icon;
              const isDefault = version.version === defaultVersion;
              const isCurrent = version.version === currentVersion;
              const isApproved = status === 'approved';
              const isDraft = status === 'draft';
              const canSetDefault = isOwner && isApproved && !isDefault;
              const canSubmit = isOwner && isDraft;
              const canRevert = isOwner && !isCurrent;
              const canCompare = version.version > 1 || defaultVersion !== version.version;

              return (
                <div
                  key={version.version}
                  className={`flex items-start gap-3 rounded-lg border p-3 ${
                    isDefault ? 'border-primary bg-primary/5' : ''
                  }`}
                >
                  <div className="flex flex-col items-center gap-1">
                    <div
                      className={`flex h-8 w-8 items-center justify-center rounded-full ${
                        isDefault
                          ? 'bg-primary text-primary-foreground'
                          : 'bg-muted text-muted-foreground'
                      }`}
                    >
                      {version.version}
                    </div>
                    {isDefault && (
                      <Star className="h-3 w-3 text-primary fill-primary" />
                    )}
                    {/* Show hash for draft/pending or release number for approved */}
                    {version.status === 'approved' && version.release_number && (
                      <span className="text-xs font-medium text-muted-foreground">
                        v{version.release_number}
                      </span>
                    )}
                    {(version.status === 'draft' || version.status === 'pending_approval') && version.version_hash && (
                      <span className="text-xs font-mono text-muted-foreground">
                        {version.version_hash}
                      </span>
                    )}
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Badge variant={statusInfo.variant} className="gap-1">
                        <StatusIcon className="h-3 w-3" />
                        {statusInfo.label}
                      </Badge>
                      {isDefault && (
                        <Badge variant="outline" className="gap-1">
                          <Star className="h-3 w-3" />
                          Default
                        </Badge>
                      )}
                    </div>

                    {version.change_summary && (
                      <p className="mt-1 text-sm text-muted-foreground line-clamp-2">
                        {version.change_summary}
                      </p>
                    )}

                    <p className="mt-1 text-xs text-muted-foreground">
                      {formatDate(version.created_at)}
                    </p>

                    {status === 'rejected' && version.rejection_reason && (
                      <div className="mt-2 rounded-md bg-destructive/10 px-2 py-1 text-xs text-destructive">
                        <strong>Rejected:</strong> {version.rejection_reason}
                      </div>
                    )}
                  </div>

                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon" className="h-8 w-8">
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
                      {version.version > 1 && (
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
                            {submitMutation.isPending ? (
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            ) : (
                              <Clock className="mr-2 h-4 w-4" />
                            )}
                            Submit for approval
                          </DropdownMenuItem>
                        </>
                      )}
                      {version.status !== "approved" && (
                        <>
                          <Separator className="my-1" />
                          <DropdownMenuItem
                            onClick={() => setDeleteVersionNum(version.version)}
                            disabled={deleteMutation.isPending}
                            className="text-red-600 focus:text-red-600"
                          >
                            {deleteMutation.isPending ? (
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            ) : (
                              <Trash2 className="mr-2 h-4 w-4" />
                            )}
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
                            Revert to this version
                          </DropdownMenuItem>
                        </>
                      )}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              );
            })}
        </div>
      </ScrollArea>

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
              Add a summary of the changes in this version.
            </DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <Label htmlFor="change-summary">Change Summary</Label>
            <Textarea
              id="change-summary"
              placeholder="Describe what changed in this version..."
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
                setSubmitChangeSummary("");
              }}
            >
              Cancel
            </Button>
            <Button
              onClick={() => {
                if (submitVersion !== null) {
                  submitMutation.mutate({
                    version: submitVersion,
                    changeSummary: submitChangeSummary,
                  });
                }
              }}
              disabled={submitMutation.isPending || !submitChangeSummary.trim()}
            >
              {submitMutation.isPending ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : null}
              Submit for Approval
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Version Confirmation Dialog */}
      <Dialog open={deleteVersionNum !== null} onOpenChange={(open) => !open && setDeleteVersionNum(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Version {deleteVersionNum}?</DialogTitle>
            <DialogDescription>
              This will permanently delete this version. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteVersionNum(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (deleteVersionNum !== null) {
                  deleteMutation.mutate(deleteVersionNum);
                }
              }}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : null}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
