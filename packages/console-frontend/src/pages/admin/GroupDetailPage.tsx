import { useState } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { ArrowLeft, Plus, X, Save, UserPlus, ExternalLink, Server, Trash2, Globe, Search } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import { cn, getErrorMessage } from '@/lib/utils';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { config } from '@/config';
import { client } from '@/api/generated/client.gen';
import {
  getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGet,
  getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGet,
} from '@/api/generated/sdk.gen';
import { totalCountFrom } from '@/api/total-count';
import {
  getGroupApiV1AdminGroupsGroupIdGetOptions,
  getGroupApiV1GroupsGroupIdGetOptions,
  updateGroupApiV1AdminGroupsGroupIdPutMutation,
  listMembersApiV1GroupsGroupIdMembersGetOptions,
  addMembersApiV1GroupsGroupIdMembersPostMutation,
  removeMembersApiV1GroupsGroupIdMembersRemovePostMutation,
  updateMemberRoleApiV1GroupsGroupIdMembersUserIdPutMutation,
  listUsersApiV1AdminUsersGetOptions,
  getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGetQueryKey,
  addGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdPostMutation,
  removeGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdDeleteMutation,
  getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGetQueryKey,
  schedulerAddGroupDefaultJobMutation,
  schedulerRemoveGroupDefaultJobMutation,
  consoleListMcpServersOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { RoleEnum, McpGatewayStatusResponse, McpGatewayServerPermissionsResponse } from '@/api/generated';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';
import { Checkbox } from '@/components/ui/checkbox';
import { Skeleton } from '@/components/ui/skeleton';
import { Badge } from '@/components/ui/badge';
import { Pagination } from '@/components/admin/Pagination';

const USER_PAGE_SIZE = 20;
const ACCESSIBLE_PAGE_SIZE = 20;

export function GroupDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isAdmin, adminMode } = useAuth();
  const groupId = parseInt(id!);
  const isAdminView = isAdmin && adminMode;
  const backPath = isAdminView ? '/app/admin/groups' : '/app/groups';

  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');

  const [addMemberDialogOpen, setAddMemberDialogOpen] = useState(false);
  const [selectedUsersToAdd, setSelectedUsersToAdd] = useState<Set<string>>(new Set());
  const [newMemberRole, setNewMemberRole] = useState<RoleEnum>('read');
  const [userSearch, setUserSearch] = useState('');
  const [userPage, setUserPage] = useState(1);
  const debouncedUserSearch = useDebouncedValue(userSearch);

  const [selectedMembersToRemove, setSelectedMembersToRemove] = useState<Set<string>>(new Set());
  const [confirmRemoveMembers, setConfirmRemoveMembers] = useState(false);

  const [membersPage, setMembersPage] = useState(1);
  const [memberSearch, setMemberSearch] = useState('');
  const debouncedMemberSearch = useDebouncedValue(memberSearch);

  // A new term re-pages from the start, otherwise a narrow search lands on an
  // empty page 4 of the previous one. Selection is dropped with it, since the
  // rows it referred to are no longer on screen to be unticked.
  const handleMemberSearchChange = (value: string) => {
    setMemberSearch(value);
    setMembersPage(1);
    setSelectedMembersToRemove(new Set());
  };

  const handleUserSearchChange = (value: string) => {
    setUserSearch(value);
    setUserPage(1);
  };

  const [agentsPage, setAgentsPage] = useState(1);
  const [agentSearch, setAgentSearch] = useState('');
  const debouncedAgentSearch = useDebouncedValue(agentSearch);
  const handleAgentSearchChange = (value: string) => {
    setAgentSearch(value);
    setAgentsPage(1);
  };

  const [jobsPage, setJobsPage] = useState(1);
  const [jobSearch, setJobSearch] = useState('');
  const debouncedJobSearch = useDebouncedValue(jobSearch);
  const handleJobSearchChange = (value: string) => {
    setJobSearch(value);
    setJobsPage(1);
  };

  // MCP Gateway server access state
  const [grantServerDialogOpen, setGrantServerDialogOpen] = useState(false);
  const [selectedServerSlug, setSelectedServerSlug] = useState('');
  const [selectedServerRole, setSelectedServerRole] = useState<'admin' | 'maintainer' | 'member'>('member');

  // Admin endpoint - full access
  const { data: adminGroupData, isLoading: isLoadingAdmin } = useQuery({
    ...getGroupApiV1AdminGroupsGroupIdGetOptions({
      path: { group_id: groupId },
    }),
    enabled: !isNaN(groupId) && isAdminView,
  });

  // Group manager endpoint - restricted access
  const { data: groupManagerData, isLoading: isLoadingManager } = useQuery({
    ...getGroupApiV1GroupsGroupIdGetOptions({
      path: { group_id: groupId },
    }),
    enabled: !isNaN(groupId) && !isAdminView,
  });

  const isLoading = isAdminView ? isLoadingAdmin : isLoadingManager;
  const groupData = isAdminView ? adminGroupData : groupManagerData;

  const { data: membersData, isLoading: membersLoading } = useQuery({
    ...listMembersApiV1GroupsGroupIdMembersGetOptions({
      path: { group_id: groupId },
      query: { page: membersPage, limit: 20, search: debouncedMemberSearch || undefined },
    }),
    enabled: !isNaN(groupId),
    placeholderData: keepPreviousData,
  });

  // The candidate list is narrowed by the server: filtering a single page of
  // users client-side would both miss people past the page and offer members
  // the current members page happens not to show.
  const { data: usersData, isLoading: usersLoading, isFetching: usersFetching } = useQuery({
    ...listUsersApiV1AdminUsersGetOptions({
      query: {
        page: userPage,
        limit: USER_PAGE_SIZE,
        search: debouncedUserSearch || undefined,
        exclude_group_id: groupId,
        status: 'active',
      },
    }),
    enabled: addMemberDialogOpen && !isNaN(groupId),
    // Every keystroke is a new query key, so without this the rows blank out to
    // a loading state and the dialog collapses and re-expands on each one.
    placeholderData: keepPreviousData,
  });

  // Agents this group can reach, one server-searched page at a time. Both lists
  // below are bare arrays (agents read them too), so the count comes from
  // `X-Total-Count` — which the tanstack wrapper drops, hence the direct
  // operation calls. The trailing key element keeps this `{rows, total}` shape
  // apart from any plain-array cache entry, while the permission dialogs'
  // `{path}`-only invalidations still reach it by prefix.
  const agentsOptions = {
    path: { group_id: groupId },
    query: { page: agentsPage, limit: ACCESSIBLE_PAGE_SIZE, search: debouncedAgentSearch || undefined },
  };
  const {
    data: defaultAgentsData,
    isLoading: defaultAgentsLoading,
    isFetching: defaultAgentsFetching,
  } = useQuery({
    queryKey: [...getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGetQueryKey(agentsOptions), 'with-total'] as const,
    queryFn: async ({ signal }) => {
      const { data, response } = await getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGet({
        ...agentsOptions,
        signal,
        throwOnError: true,
      });
      return { rows: data, total: totalCountFrom(response, data.length) };
    },
    enabled: !isNaN(groupId),
    placeholderData: keepPreviousData,
  });

  // Scheduled jobs shared with this group, flagged with which are its defaults
  // (ADR-0010). Sharing a job is done from the job's own page; this is where a
  // manager decides whether the whole group runs it.
  const jobsOptions = {
    path: { group_id: groupId },
    query: { page: jobsPage, limit: ACCESSIBLE_PAGE_SIZE, search: debouncedJobSearch || undefined },
  };
  const {
    data: accessibleJobsData,
    isLoading: accessibleJobsLoading,
    isFetching: accessibleJobsFetching,
  } = useQuery({
    queryKey: [...getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGetQueryKey(jobsOptions), 'with-total'] as const,
    queryFn: async ({ signal }) => {
      const { data, response } = await getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGet({
        ...jobsOptions,
        signal,
        throwOnError: true,
      });
      return { rows: data, total: totalCountFrom(response, data.length) };
    },
    enabled: !isNaN(groupId),
    placeholderData: keepPreviousData,
  });

  // MCP Gateway server access queries
  const { data: gatewayStatus } = useQuery<McpGatewayStatusResponse>({
    queryKey: ['mcpGatewayStatus', groupId],
    queryFn: async () => {
      const res = await client.get<McpGatewayStatusResponse>({
        url: `/api/v1/admin/groups/${groupId}/mcp-gateway-status`,
      });
      return res.data as unknown as McpGatewayStatusResponse;
    },
    enabled: !isNaN(groupId) && isAdminView,
  });

  const { data: gatewayServers, isLoading: gatewayServersLoading } = useQuery<McpGatewayServerPermissionsResponse>({
    queryKey: ['mcpGatewayServers', groupId],
    queryFn: async () => {
      const res = await client.get<McpGatewayServerPermissionsResponse>({
        url: `/api/v1/admin/groups/${groupId}/mcp-gateway-servers`,
      });
      return res.data as unknown as McpGatewayServerPermissionsResponse;
    },
    enabled: !isNaN(groupId) && isAdminView && gatewayStatus?.managed === true,
  });

  const { data: availableServersData } = useQuery({
    ...consoleListMcpServersOptions(),
    enabled: !isNaN(groupId) && isAdminView && gatewayStatus?.managed === true,
  });

  const updateMutation = useMutation({
    ...updateGroupApiV1AdminGroupsGroupIdPutMutation(),
    onSuccess: () => {
      toast.success('Group updated successfully');
      setIsEditing(false);
      queryClient.invalidateQueries({
        queryKey: getGroupApiV1AdminGroupsGroupIdGetOptions({
          path: { group_id: groupId },
        }).queryKey,
      });
      queryClient.invalidateQueries({
        predicate: (query) => query.queryKey[0] === 'listGroupsApiV1AdminGroupsGet',
      });
    },
    onError: (error: any) => {
      const message = error?.detail || error?.response?.data?.detail || 'Failed to update group';
      toast.error(message);
    },
  });

  const addMembersMutation = useMutation({
    ...addMembersApiV1GroupsGroupIdMembersPostMutation(),
    onSuccess: () => {
      toast.success('Members added successfully');
      setAddMemberDialogOpen(false);
      setSelectedUsersToAdd(new Set());
      setUserSearch('');
      setUserPage(1);
      queryClient.invalidateQueries({
        queryKey: listMembersApiV1GroupsGroupIdMembersGetOptions({
          path: { group_id: groupId },
          query: { page: membersPage, limit: 20 },
        }).queryKey,
      });
      queryClient.invalidateQueries({
        queryKey: getGroupApiV1AdminGroupsGroupIdGetOptions({
          path: { group_id: groupId },
        }).queryKey,
      });
    },
    onError: (error: any) => {
      const message = error?.detail || error?.response?.data?.detail || 'Failed to add members';
      toast.error(message);
    },
  });

  const removeMembersMutation = useMutation({
    ...removeMembersApiV1GroupsGroupIdMembersRemovePostMutation(),
    onSuccess: () => {
      toast.success(`Removed ${selectedMembersToRemove.size} member(s)`);
      setSelectedMembersToRemove(new Set());
      queryClient.invalidateQueries({
        queryKey: listMembersApiV1GroupsGroupIdMembersGetOptions({
          path: { group_id: groupId },
          query: { page: membersPage, limit: 20 },
        }).queryKey,
      });
      queryClient.invalidateQueries({
        queryKey: getGroupApiV1AdminGroupsGroupIdGetOptions({
          path: { group_id: groupId },
        }).queryKey,
      });
    },
    onError: (error: any) => {
      const detail = error?.detail || error?.response?.data?.detail || error?.message;
      let message = 'Failed to remove members';

      if (detail) {
        message = detail;
      }

      toast.error(message);
    },
  });

  const updateRoleMutation = useMutation({
    ...updateMemberRoleApiV1GroupsGroupIdMembersUserIdPutMutation(),
    onSuccess: () => {
      toast.success('Member role updated');
      queryClient.invalidateQueries({
        queryKey: listMembersApiV1GroupsGroupIdMembersGetOptions({
          path: { group_id: groupId },
          query: { page: membersPage, limit: 20 },
        }).queryKey,
      });
    },
    onError: (error: any) => {
      const message = error?.detail || error?.response?.data?.detail || 'Failed to update role';
      toast.error(message);
    },
  });

  // Defaults are toggled one agent at a time rather than by writing the whole
  // set: the list is paged, so the set this page knows about is not the group's.
  const invalidateAccessibleAgents = () =>
    queryClient.invalidateQueries({
      queryKey: getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGetQueryKey({
        path: { group_id: groupId },
      }),
    });
  const onDefaultAgentError = (error: unknown) =>
    toast.error('Failed to update default agent', { description: getErrorMessage(error) });
  const addDefaultAgentMutation = useMutation({
    ...addGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdPostMutation(),
    onSuccess: () => {
      toast.success('Default agent status updated');
      invalidateAccessibleAgents();
    },
    onError: onDefaultAgentError,
  });
  const removeDefaultAgentMutation = useMutation({
    ...removeGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdDeleteMutation(),
    onSuccess: () => {
      toast.success('Default agent status updated');
      invalidateAccessibleAgents();
    },
    onError: onDefaultAgentError,
  });
  const defaultAgentPending = addDefaultAgentMutation.isPending || removeDefaultAgentMutation.isPending;

  const grantServerAccessMutation = useMutation({
    mutationFn: async ({ serverSlug, role }: { serverSlug: string; role: string }) => {
      await client.put({
        url: `/api/v1/admin/groups/${groupId}/mcp-gateway-servers/${serverSlug}`,
        body: { role },
      });
    },
    onSuccess: () => {
      toast.success('Server access granted');
      setGrantServerDialogOpen(false);
      setSelectedServerSlug('');
      setSelectedServerRole('member');
      queryClient.invalidateQueries({ queryKey: ['mcpGatewayServers', groupId] });
    },
    onError: (error: any) => {
      const message = error?.detail || error?.response?.data?.detail || 'Failed to grant server access';
      toast.error(message);
    },
  });

  const revokeServerAccessMutation = useMutation({
    mutationFn: async (serverSlug: string) => {
      await client.delete({
        url: `/api/v1/admin/groups/${groupId}/mcp-gateway-servers/${serverSlug}`,
      });
    },
    onSuccess: () => {
      toast.success('Server access revoked');
      queryClient.invalidateQueries({ queryKey: ['mcpGatewayServers', groupId] });
    },
    onError: (error: any) => {
      const message = error?.detail || error?.response?.data?.detail || 'Failed to revoke server access';
      toast.error(message);
    },
  });

  const group = groupData?.data;
  const members = membersData?.data ?? [];
  const membersMeta = membersData?.meta ?? { page: 1, limit: 20, total: 0 };
  // Already filtered server-side to active non-members (see the query above).
  const availableUsers = usersData?.data ?? [];
  const availableUsersMeta = usersData?.meta ?? { page: 1, limit: USER_PAGE_SIZE, total: 0 };

  // The members list total follows its search box, so the group's own count is
  // what a "this reaches N people" confirmation must quote.
  const groupMemberCount = group?.member_count ?? membersMeta.total;

  const accessibleAgents = defaultAgentsData?.rows ?? [];
  const accessibleAgentsTotal = defaultAgentsData?.total ?? 0;
  const accessibleJobs = accessibleJobsData?.rows ?? [];
  const accessibleJobsTotal = accessibleJobsData?.total ?? 0;

  const gatewayPermissions = gatewayServers?.permissions ?? [];
  const grantedSlugs = new Set(gatewayPermissions.map((p) => p.server_slug));
  // All servers from gateway — "name" field is actually the slug
  const allGatewayServers = (availableServersData?.servers ?? []) as Array<{
    name: string;
    description?: string | null;
    visibility?: string | null;
  }>;
  // Organization-visibility servers are always shown (available to everyone)
  const orgServers = allGatewayServers.filter((s) => s.visibility === 'organization' && s.name !== 'console');
  const orgServerSlugs = new Set(orgServers.map((s) => s.name));
  // Available for granting: private servers not already granted and not org-level
  const availableServers = allGatewayServers
    .filter((s) => s.name !== 'console')
    .filter((s) => s.visibility !== 'organization')
    .filter((s) => !grantedSlugs.has(s.name));

  const startEditing = () => {
    if (!group) return;
    setEditName(group.name);
    setEditDescription(group.description ?? '');
    setIsEditing(true);
  };

  const handleSave = () => {
    updateMutation.mutate({
      path: { group_id: groupId },
      body: {
        name: editName,
        description: editDescription || null,
      },
    });
  };

  const handleAddMembers = () => {
    if (selectedUsersToAdd.size === 0) return;
    addMembersMutation.mutate({
      path: { group_id: groupId },
      body: {
        user_ids: Array.from(selectedUsersToAdd),
        role: newMemberRole,
      },
    });
  };

  const handleRemoveSelectedMembers = () => {
    if (selectedMembersToRemove.size === 0) return;
    removeMembersMutation.mutate({
      path: { group_id: groupId },
      body: {
        user_ids: Array.from(selectedMembersToRemove),
      },
    });
  };

  const handleRoleChange = (userId: string, role: RoleEnum) => {
    updateRoleMutation.mutate({
      path: { group_id: groupId, user_id: userId },
      body: { role },
    });
  };

  // Turning a job on for the whole group starts a run per member under their own
  // identity, so the confirmation names the number of people rather than the job.
  const [pendingDefaultJob, setPendingDefaultJob] = useState<{ id: number; name: string } | null>(
    null,
  );

  // Per-job toggles, for the same reason as the agents above: a paged list
  // cannot rewrite the whole default set without dropping the unseen rows.
  const invalidateAccessibleJobs = () =>
    queryClient.invalidateQueries({
      queryKey: getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGetQueryKey({
        path: { group_id: groupId },
      }),
    });
  const addDefaultJobMutation = useMutation({
    ...schedulerAddGroupDefaultJobMutation(),
    onSuccess: () => {
      invalidateAccessibleJobs();
      toast.success('Default jobs updated');
    },
    onError: (err) => toast.error('Could not update default jobs', { description: String(err) }),
  });
  const removeDefaultJobMutation = useMutation({
    ...schedulerRemoveGroupDefaultJobMutation(),
    onSuccess: () => {
      invalidateAccessibleJobs();
      toast.success('Default jobs updated');
    },
    onError: (err) => toast.error('Could not update default jobs', { description: String(err) }),
  });
  const defaultJobPending = addDefaultJobMutation.isPending || removeDefaultJobMutation.isPending;

  const handleToggleDefaultJob = (jobId: number, currentlyDefault: boolean, jobName: string) => {
    if (currentlyDefault) {
      removeDefaultJobMutation.mutate({ path: { group_id: groupId, definition_id: jobId } });
      return;
    }
    setPendingDefaultJob({ id: jobId, name: jobName });
  };

  const handleToggleDefault = (agentId: number, currentlyDefault: boolean) => {
    const path = { group_id: groupId, sub_agent_id: agentId };
    if (currentlyDefault) {
      removeDefaultAgentMutation.mutate({ path });
    } else {
      addDefaultAgentMutation.mutate({ path });
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-6 p-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (!group) {
    return (
      <div className="text-center py-12 p-4">
        <p className="text-muted-foreground">Group not found</p>
        <Button variant="link" onClick={() => navigate(backPath)}>
          Back to Groups
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6 p-4">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={() => navigate(backPath)}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="flex-1">
          <h1 className="text-2xl font-bold tracking-tight">{group.name}</h1>
          <p className="text-muted-foreground">{group.description || 'No description'}</p>
        </div>
        {isAdminView && group.keycloak_group_id && (
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              window.open(
                `${config.keycloakBaseUrl}/admin/master/console/#/${config.keycloakRealm}/groups/${group.keycloak_group_id}`,
                '_blank'
              )
            }
          >
            <ExternalLink className="h-4 w-4 mr-2" />
            Open in Keycloak
          </Button>
        )}
        {!isEditing && isAdminView && <Button onClick={startEditing}>Edit Group</Button>}
      </div>

      {isEditing && isAdminView ? (
        <Card>
          <CardHeader>
            <CardTitle>Edit Group</CardTitle>
            <CardDescription>Update group details and permissions</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="name">Name</Label>
                <Input id="name" value={editName} onChange={(e) => setEditName(e.target.value)} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="description">Description</Label>
                <Textarea
                  id="description"
                  value={editDescription}
                  onChange={(e) => setEditDescription(e.target.value)}
                />
              </div>
            </div>

            <div className="flex gap-2">
              <Button onClick={handleSave} disabled={updateMutation.isPending}>
                <Save className="h-4 w-4 mr-2" />
                {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
              </Button>
              <Button variant="outline" onClick={() => setIsEditing(false)}>
                Cancel
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Group Details</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <p className="text-sm text-muted-foreground">Name</p>
                <p className="font-medium">{group.name}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Description</p>
                <p className="font-medium">{group.description || '-'}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Member Count</p>
                <p className="font-medium">{group.member_count ?? 0}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Created</p>
                <p className="font-medium">
                  {group.created_at ? new Date(group.created_at).toLocaleDateString() : '-'}
                </p>
              </div>
            </div>

            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">Access Control</p>
              <p className="text-sm text-muted-foreground">
                Permissions are managed through user system roles and group member roles. Members can have Read, Write,
                or Manager access to group resources.
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <div>
            <CardTitle>Accessible Agents</CardTitle>
            <CardDescription>
              All approved agents this group can access. Toggle to set as default for new members.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <div className="relative mb-4">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search agents by name or description..."
              value={agentSearch}
              onChange={(e) => handleAgentSearchChange(e.target.value)}
              className="pl-9"
            />
          </div>
          <div
            className={cn(
              'border rounded-lg transition-opacity',
              defaultAgentsFetching && !defaultAgentsLoading && 'opacity-60',
            )}
          >
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-16">Default</TableHead>
                  <TableHead>Agent Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Owner</TableHead>
                  <TableHead className="w-20">Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {defaultAgentsLoading ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center py-8">
                      Loading...
                    </TableCell>
                  </TableRow>
                ) : accessibleAgents.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">
                      {debouncedAgentSearch
                        ? 'No agents match your search'
                        : 'No accessible agents. Add permissions first.'}
                    </TableCell>
                  </TableRow>
                ) : (
                  accessibleAgents.map((agent: any) => (
                    <TableRow key={agent.id}>
                      <TableCell>
                        <Checkbox
                          checked={agent.is_default}
                          onCheckedChange={() => handleToggleDefault(agent.id, agent.is_default)}
                          disabled={defaultAgentPending}
                        />
                      </TableCell>
                      <TableCell className="font-medium">{agent.name}</TableCell>
                      <TableCell className="capitalize">{(agent as any).agent_type || '-'}</TableCell>
                      <TableCell>{(agent as any).owner_email || '-'}</TableCell>
                      <TableCell>
                        {agent.is_activated ? (
                          <span className="text-green-600 text-sm">Active</span>
                        ) : (
                          <span className="text-muted-foreground text-sm">Inactive</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
          <Pagination
            page={agentsPage}
            limit={ACCESSIBLE_PAGE_SIZE}
            total={accessibleAgentsTotal}
            onPageChange={setAgentsPage}
          />
        </CardContent>
      </Card>

      {/* Default scheduled jobs (ADR-0010). Same shape as the agents card above, and
          deliberately so: a job shared with the group is activated for every member the
          same way a default agent is. Unlike agents there is no approval state — a job
          either reaches this group or it does not. */}
      <Card>
        <CardHeader>
          <div>
            <CardTitle>Accessible Scheduled Jobs</CardTitle>
            <CardDescription>
              Jobs shared with this group. Making one a default activates it for every current
              and future member, each running it under their own account.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <div className="relative mb-4">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search jobs by name or prompt..."
              value={jobSearch}
              onChange={(e) => handleJobSearchChange(e.target.value)}
              className="pl-9"
            />
          </div>
          <div
            className={cn(
              'border rounded-lg transition-opacity',
              accessibleJobsFetching && !accessibleJobsLoading && 'opacity-60',
            )}
          >
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-16">Default</TableHead>
                  <TableHead>Job Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Owner</TableHead>
                  <TableHead className="w-24">Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {accessibleJobsLoading ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center py-8">
                      Loading...
                    </TableCell>
                  </TableRow>
                ) : accessibleJobs.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">
                      {debouncedJobSearch
                        ? 'No jobs match your search'
                        : 'No jobs are shared with this group. Share one from its own page in the Scheduler first.'}
                    </TableCell>
                  </TableRow>
                ) : (
                  accessibleJobs.map((job) => (
                    <TableRow key={job.id}>
                      <TableCell>
                        <Checkbox
                          checked={job.is_default}
                          onCheckedChange={() =>
                            handleToggleDefaultJob(job.id, !!job.is_default, job.name)
                          }
                          disabled={defaultJobPending}
                        />
                      </TableCell>
                      <TableCell className="font-medium">{job.name}</TableCell>
                      <TableCell className="capitalize">{job.job_type}</TableCell>
                      <TableCell>{job.owner_user_id}</TableCell>
                      <TableCell>
                        {job.suspended ? (
                          <Badge variant="secondary">Suspended</Badge>
                        ) : (
                          <span className="text-muted-foreground text-sm">—</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
          <Pagination
            page={jobsPage}
            limit={ACCESSIBLE_PAGE_SIZE}
            total={accessibleJobsTotal}
            onPageChange={setJobsPage}
          />
        </CardContent>
      </Card>

      <ConfirmDialog
        open={pendingDefaultJob !== null}
        onOpenChange={(open) => !open && setPendingDefaultJob(null)}
        title="Activate this job for everyone in the group?"
        description={`"${pendingDefaultJob?.name}" will start running for ${
          groupMemberCount === 1 ? 'the 1 member' : `all ${groupMemberCount} members`
        } of ${group?.name}, each under their own account and using their own credentials. They will be told, and can turn it off.`}
        confirmLabel="Activate"
        onConfirm={() => {
          if (pendingDefaultJob) {
            addDefaultJobMutation.mutate({
              path: { group_id: groupId, definition_id: pendingDefaultJob.id },
            });
          }
          setPendingDefaultJob(null);
        }}
      />

      {/* MCP Gateway Server Access */}
      {isAdminView && gatewayStatus?.managed && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle className="flex items-center gap-2">
                <Server className="h-5 w-5" />
                MCP Gateway Servers
              </CardTitle>
              <CardDescription>Manage which MCP gateway servers this group can access.</CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="secondary">Managed</Badge>
              <Button size="sm" onClick={() => setGrantServerDialogOpen(true)}>
                <Plus className="h-4 w-4 mr-2" />
                Grant Access
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <div className="border rounded-lg">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Server</TableHead>
                    <TableHead>Role</TableHead>
                    <TableHead className="w-20"></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {gatewayServersLoading ? (
                    <TableRow>
                      <TableCell colSpan={3} className="text-center py-8">
                        Loading...
                      </TableCell>
                    </TableRow>
                  ) : gatewayPermissions.length === 0 && orgServers.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={3} className="text-center py-8 text-muted-foreground">
                        No server access configured.
                      </TableCell>
                    </TableRow>
                  ) : (
                    <>
                      {orgServers.map((server) => (
                        <TableRow key={server.name} className="bg-muted/30">
                          <TableCell className="font-medium">
                            <span className="flex items-center gap-2">
                              <Globe className="h-3.5 w-3.5 text-muted-foreground" />
                              {server.name}
                            </span>
                          </TableCell>
                          <TableCell>
                            <Badge variant="secondary">organization</Badge>
                          </TableCell>
                          <TableCell />
                        </TableRow>
                      ))}
                      {gatewayPermissions
                        .filter((perm) => !orgServerSlugs.has(perm.server_slug))
                        .map((perm) => (
                          <TableRow key={perm.server_slug}>
                            <TableCell className="font-medium">{perm.server_slug}</TableCell>
                            <TableCell>
                              <Badge variant="outline">{perm.role}</Badge>
                            </TableCell>
                            <TableCell>
                              <Button
                                variant="ghost"
                                size="icon"
                                aria-label="Revoke access"
                                onClick={() => revokeServerAccessMutation.mutate(perm.server_slug)}
                                disabled={revokeServerAccessMutation.isPending}
                              >
                                <Trash2 className="h-4 w-4 text-destructive" />
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))}
                    </>
                  )}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Members</CardTitle>
            <CardDescription>Manage group membership</CardDescription>
          </div>
          <div className="flex gap-2">
            {selectedMembersToRemove.size > 0 && (
              <Button
                variant="destructive"
                onClick={() => setConfirmRemoveMembers(true)}
                disabled={removeMembersMutation.isPending}
              >
                <X className="h-4 w-4 mr-2" />
                {removeMembersMutation.isPending ? 'Removing...' : `Remove ${selectedMembersToRemove.size} Selected`}
              </Button>
            )}
            <Button onClick={() => setAddMemberDialogOpen(true)}>
              <UserPlus className="h-4 w-4 mr-2" />
              Add Members
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="relative mb-4">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search members by name or email..."
              value={memberSearch}
              onChange={(e) => handleMemberSearchChange(e.target.value)}
              className="pl-9"
            />
          </div>
          <div className="border rounded-lg">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12">
                    <Checkbox
                      checked={members.length > 0 && selectedMembersToRemove.size === members.length}
                      onCheckedChange={(checked) => {
                        if (checked) {
                          setSelectedMembersToRemove(new Set(members.map((m) => m.user_id)));
                        } else {
                          setSelectedMembersToRemove(new Set());
                        }
                      }}
                    />
                  </TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Email</TableHead>
                  <TableHead>Role</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {membersLoading ? (
                  <TableRow>
                    <TableCell colSpan={4} className="text-center py-8">
                      Loading...
                    </TableCell>
                  </TableRow>
                ) : members.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={4} className="text-center py-8 text-muted-foreground">
                      {debouncedMemberSearch ? 'No members match your search' : 'No members'}
                    </TableCell>
                  </TableRow>
                ) : (
                  members.map((member) => (
                    <TableRow key={member.user_id}>
                      <TableCell>
                        <Checkbox
                          checked={selectedMembersToRemove.has(member.user_id)}
                          onCheckedChange={(checked) => {
                            const newSet = new Set(selectedMembersToRemove);
                            if (checked) {
                              newSet.add(member.user_id);
                            } else {
                              newSet.delete(member.user_id);
                            }
                            setSelectedMembersToRemove(newSet);
                          }}
                        />
                      </TableCell>
                      <TableCell className="font-medium">
                        {member.first_name} {member.last_name}
                      </TableCell>
                      <TableCell>{member.email}</TableCell>
                      <TableCell>
                        <Select
                          value={member.group_role}
                          onValueChange={(value) => handleRoleChange(member.user_id, value as RoleEnum)}
                        >
                          <SelectTrigger className="w-32">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="read">Read</SelectItem>
                            <SelectItem value="write">Write</SelectItem>
                            <SelectItem value="manager">Manager</SelectItem>
                          </SelectContent>
                        </Select>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
          <Pagination
            page={membersMeta.page}
            limit={membersMeta.limit}
            total={membersMeta.total}
            onPageChange={setMembersPage}
          />
        </CardContent>
      </Card>

      {/* Add Members Dialog */}
      <Dialog
        open={addMemberDialogOpen}
        onOpenChange={(open) => {
          setAddMemberDialogOpen(open);
          if (!open) {
            setUserSearch('');
            setUserPage(1);
            setSelectedUsersToAdd(new Set());
          }
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Add Members</DialogTitle>
            <DialogDescription>Select users to add to this group.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>Role</Label>
              <Select value={newMemberRole} onValueChange={(v) => setNewMemberRole(v as RoleEnum)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="read">Read</SelectItem>
                  <SelectItem value="write">Write</SelectItem>
                  <SelectItem value="manager">Manager</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Users</Label>
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  placeholder="Search users by name or email..."
                  value={userSearch}
                  onChange={(e) => handleUserSearchChange(e.target.value)}
                  className="pl-9"
                />
              </div>
              {/* Reserved row: appearing on the first tick would shove the list
                  down by its height. */}
              <p className="text-sm text-muted-foreground min-h-[1.25rem]">
                {selectedUsersToAdd.size > 0
                  ? `${selectedUsersToAdd.size} selected. Selection is kept while you search.`
                  : ''}
              </p>
              <div
                className={cn(
                  'border rounded-lg h-64 overflow-y-auto transition-opacity',
                  // Previous rows stay put while the new term loads; dim them so
                  // the list still reads as busy without changing size.
                  usersFetching && !usersLoading && 'opacity-60',
                )}
              >
                {usersLoading ? (
                  <div className="p-4 text-center text-muted-foreground">Loading...</div>
                ) : availableUsers.length === 0 ? (
                  <div className="p-4 text-center text-muted-foreground">
                    {debouncedUserSearch ? 'No users match your search' : 'No available users to add'}
                  </div>
                ) : (
                  availableUsers.map((user) => (
                    <div key={user.id} className="flex items-center gap-3 p-3 border-b last:border-b-0">
                      <Checkbox
                        checked={selectedUsersToAdd.has(user.id)}
                        onCheckedChange={(checked) => {
                          const newSet = new Set(selectedUsersToAdd);
                          if (checked) {
                            newSet.add(user.id);
                          } else {
                            newSet.delete(user.id);
                          }
                          setSelectedUsersToAdd(newSet);
                        }}
                      />
                      <div>
                        <p className="font-medium">
                          {user.first_name} {user.last_name}
                        </p>
                        <p className="text-sm text-muted-foreground">{user.email}</p>
                      </div>
                    </div>
                  ))
                )}
              </div>
              {/* Pagination renders nothing at all when there are no results, so
                  it gets a reserved row: otherwise the dialog jumps by its
                  height every time a search empties or refills the list. */}
              <div className="min-h-[68px]">
                <Pagination
                  page={availableUsersMeta.page}
                  limit={availableUsersMeta.limit}
                  total={availableUsersMeta.total}
                  onPageChange={setUserPage}
                />
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddMemberDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleAddMembers} disabled={selectedUsersToAdd.size === 0 || addMembersMutation.isPending}>
              <Plus className="h-4 w-4 mr-1" />
              {addMembersMutation.isPending ? 'Adding...' : `Add ${selectedUsersToAdd.size} User(s)`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Grant Server Access Dialog */}
      <Dialog open={grantServerDialogOpen} onOpenChange={setGrantServerDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Grant Server Access</DialogTitle>
            <DialogDescription>Select an MCP gateway server and role to grant access to this group.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>Server</Label>
              <Select value={selectedServerSlug} onValueChange={setSelectedServerSlug}>
                <SelectTrigger>
                  <SelectValue placeholder="Select a server..." />
                </SelectTrigger>
                <SelectContent>
                  {availableServers.map((server) => (
                    <SelectItem key={server.name} value={server.name}>
                      {server.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Role</Label>
              <Select
                value={selectedServerRole}
                onValueChange={(v) => setSelectedServerRole(v as 'admin' | 'maintainer' | 'member')}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="member">Member</SelectItem>
                  <SelectItem value="maintainer">Maintainer</SelectItem>
                  <SelectItem value="admin">Admin</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setGrantServerDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() =>
                grantServerAccessMutation.mutate({ serverSlug: selectedServerSlug, role: selectedServerRole })
              }
              disabled={!selectedServerSlug || grantServerAccessMutation.isPending}
            >
              {grantServerAccessMutation.isPending ? 'Granting...' : 'Grant Access'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmRemoveMembers}
        onOpenChange={setConfirmRemoveMembers}
        title="Remove selected members?"
        description={`${selectedMembersToRemove.size} member${selectedMembersToRemove.size === 1 ? '' : 's'} will be removed from this group.`}
        confirmLabel="Remove"
        variant="destructive"
        isLoading={removeMembersMutation.isPending}
        onConfirm={() => {
          handleRemoveSelectedMembers();
          setConfirmRemoveMembers(false);
        }}
      />
    </div>
  );
}
