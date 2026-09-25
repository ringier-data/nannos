import { useState } from 'react';
import { keepPreviousData, useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
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
  Search,
  Star,
  Trash2,
  XCircle,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
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
  getSubAgentApiV1SubAgentsSubAgentIdGetOptions,
  getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGetQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import {
  getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGet,
  deleteVersionApiV1SubAgentsSubAgentIdVersionsVersionDelete,
  revertToVersionApiV1SubAgentsSubAgentIdVersionsVersionRevertPost,
  setDefaultVersionApiV1SubAgentsSubAgentIdDefaultVersionPut,
  submitVersionForApprovalApiV1SubAgentsSubAgentIdVersionsVersionSubmitPost,
} from '@/api/generated/sdk.gen';
import { ExpandableText } from './ExpandableText';
import { VersionDiffViewer } from './VersionDiffViewer';
import type { SubAgent, SubAgentConfigVersion, SubAgentStatus } from './types';
import { totalCountFrom } from '@/api/total-count';
import { useDebouncedValue } from '@/hooks/use-debounced-value';

const VERSION_PAGE_SIZE = 20;

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
  /** How many versions the sub-agent has, unsearched. The list itself is fetched here, a page at a time. */
  versionCount: number;
  isOwner: boolean;
  isAdmin: boolean;
  hasWriteAccess?: boolean;
  /** Host-published sub-agent (ADR-0006): versions are written by the sync, so revert, delete and set-default are off. */
  isEmbedBound?: boolean;
  isCollapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  onRefresh?: () => void;
  viewingVersion?: number | null;
  /** The clicked row, or null for the current version. */
  onViewVersion?: (version: SubAgentConfigVersion | null) => void;
}

export function VersionSidebar({
  subAgent,
  versionCount,
  isOwner,
  isAdmin,
  hasWriteAccess = false,
  isEmbedBound = false,
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

  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search);

  // History only grows, so it is paged, newest first, with a "load more" rather
  // than numbered pages — the sidebar is a narrow scrolling column. The body is a
  // bare array; the match count comes from `X-Total-Count`, read off the
  // generated operation because the tanstack wrapper drops the response. The
  // `_id`-predicate invalidations below (and on the page) still reach this key.
  const versionsQuery = { search: debouncedSearch || undefined, limit: VERSION_PAGE_SIZE };
  const {
    data: versionPages,
    isLoading: versionsLoading,
    isFetching: versionsFetching,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
  } = useInfiniteQuery({
    queryKey: [
      ...getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGetQueryKey({
        path: { sub_agent_id: subAgent.id },
        query: versionsQuery,
      }),
      'infinite',
    ] as const,
    queryFn: async ({ pageParam, signal }) => {
      const { data, response } = await getSubAgentVersionsApiV1SubAgentsSubAgentIdVersionsGet({
        path: { sub_agent_id: subAgent.id },
        query: { ...versionsQuery, page: pageParam },
        signal,
        throwOnError: true,
      });
      return { rows: data, total: totalCountFrom(response, data.length) };
    },
    initialPageParam: 1,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((n, p) => n + p.rows.length, 0);
      return lastPage.rows.length > 0 && loaded < lastPage.total ? allPages.length + 1 : undefined;
    },
    enabled: !isCollapsed && versionCount > 0,
    placeholderData: keepPreviousData,
  });
  const versions = versionPages?.pages.flatMap((p) => p.rows) ?? [];
  const matchCount = versionPages?.pages[0]?.total ?? 0;

  const defaultVersion = subAgent.default_version;
  const currentVersion = subAgent.current_version ?? versionCount;

  // A version by number, whether or not it is on a loaded page (or matches the
  // search). Null when it does not exist or was deleted — deleted versions stay
  // joinable by number but are gone from the history.
  const fetchVersion = async (version: number): Promise<SubAgentConfigVersion | null> => {
    const agent = await queryClient.fetchQuery(
      getSubAgentApiV1SubAgentsSubAgentIdGetOptions({
        path: { sub_agent_id: subAgent.id },
        query: { version },
      }),
    );
    const config = agent.config_version;
    return config && config.version === version && !config.deleted_at ? config : null;
  };

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

  // The default and previous versions are looked up by number rather than in
  // `versions`: that is only the loaded pages, narrowed by any search.
  const handleCompareWithDefault = async (version: SubAgentConfigVersion) => {
    let defaultVer: SubAgentConfigVersion | null = null;
    if (defaultVersion != null) {
      try {
        defaultVer = versions.find((v) => v.version === defaultVersion) ?? (await fetchVersion(defaultVersion));
      } catch {
        defaultVer = null;
      }
    }
    setCompareVersions({ from: defaultVer, to: version });
    setDiffDialogOpen(true);
  };

  const handleCompareWithPrevious = async (version: SubAgentConfigVersion) => {
    // The actual previous version, not just version - 1: intermediate versions
    // may have been deleted. Unsearched, the loaded rows are a contiguous newest-
    // first run, so the next row is it when loaded; otherwise walk down by number.
    let prevVersion: SubAgentConfigVersion | null = null;
    const index = versions.findIndex((v) => v.version === version.version);
    if (!debouncedSearch && index >= 0 && index + 1 < versions.length) {
      prevVersion = versions[index + 1];
    } else {
      try {
        for (let n = version.version - 1; n >= 1 && !prevVersion; n--) {
          prevVersion = await fetchVersion(n);
        }
      } catch {
        prevVersion = null;
      }
    }

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

  if (versionCount === 0) {
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
              {debouncedSearch ? `${matchCount} of ${versionCount}` : versionCount}
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

        <div className="px-3 pt-3 shrink-0">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
            <Input
              placeholder="Search summary or hash..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 pl-8 text-xs"
            />
          </div>
        </div>

        <ScrollArea className="flex-1 min-h-0">
          <div
            className={`p-3 space-y-2 transition-opacity ${
              versionsFetching && !versionsLoading && !isFetchingNextPage ? 'opacity-60' : ''
            }`}
          >
            {versionsLoading && (
              <div className="flex justify-center py-6">
                <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
              </div>
            )}
            {!versionsLoading && versions.length === 0 && (
              <p className="py-6 text-center text-xs text-muted-foreground">
                {debouncedSearch ? 'No versions match your search' : 'No versions'}
              </p>
            )}
            {versions
              .map((version) => {
                const status = version.status ?? 'draft';
                const statusInfo = statusConfig[status];
                const StatusIcon = statusInfo.icon;
                const isDefault = version.version === defaultVersion;
                const isCurrent = version.version === currentVersion;
                const isViewing = viewingVersion === version.version || (viewingVersion === null && isCurrent);
                const isApproved = status === 'approved';
                const isDraft = status === 'draft';
                const isRejected = status === 'rejected';
                // Owners and admins (in admin mode) can manage versions.
                // Group members with write access can also create/submit drafts.
                const canManage = isOwner || isAdmin;
                const canWrite = canManage || hasWriteAccess;
                const canSetDefault = canWrite && !isEmbedBound && isApproved && !isDefault;
                const canSubmit = canWrite && (isDraft || isRejected);
                const canRevert = canWrite && !isEmbedBound && !isCurrent;
                const canCompare = version.version > 1 || (defaultVersion !== undefined && defaultVersion !== version.version);
                // Version 1 has nothing before it; for any other, deleted
                // predecessors are found (or reported missing) on click.
                const hasPreviousVersion = version.version > 1;
                // Can delete non-approved versions (except if it's the only version)
                const canDelete = canManage && !isEmbedBound && !isApproved && versionCount > 1;

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
                    onClick={() => onViewVersion?.(isCurrent ? null : version)}
                  >
                    {/* Version identifier and current indicator */}
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        {/* Show release number for approved, hash for others */}
                        {isApproved && version.release_number ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <div
                                className={`flex h-7 min-w-7 px-2 items-center justify-center rounded-full text-sm font-medium ${
                                  isViewing
                                    ? 'bg-blue-500 text-white'
                                    : isDefault
                                      ? 'bg-primary text-primary-foreground'
                                      : 'bg-muted text-muted-foreground'
                                }`}
                              >
                                v{version.release_number}
                              </div>
                            </TooltipTrigger>
                            <TooltipContent>{`Release ${version.release_number}`}</TooltipContent>
                          </Tooltip>
                        ) : (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <div
                                className={`flex h-7 px-2 items-center justify-center rounded text-xs font-mono ${
                                  isViewing
                                    ? 'bg-blue-500 text-white'
                                    : 'bg-muted text-muted-foreground'
                                }`}
                              >
                                {version.version_hash ? `#${version.version_hash.slice(0, 7)}` : `v${version.version}`}
                              </div>
                            </TooltipTrigger>
                            <TooltipContent>{version.version_hash || `Version ${version.version}`}</TooltipContent>
                          </Tooltip>
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
                      <ExpandableText
                        text={version.change_summary}
                        className="text-xs text-muted-foreground mb-1"
                      />
                    )}

                    {/* Date */}
                    <p className="text-xs text-muted-foreground">
                      {formatDate(version.created_at)}
                    </p>

                    {/* Rejection reason */}
                    {status === 'rejected' && version.rejection_reason && (
                      <div className="mt-2 rounded-md bg-destructive/10 px-2 py-1 text-xs text-destructive wrap-anywhere">
                        <strong>Rejected:</strong> {version.rejection_reason}
                      </div>
                    )}
                  </div>
                );
              })}
            {hasNextPage && (
              <Button
                variant="ghost"
                size="sm"
                className="w-full text-xs"
                onClick={() => fetchNextPage()}
                disabled={isFetchingNextPage}
              >
                {isFetchingNextPage && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}
                Load older versions
              </Button>
            )}
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
