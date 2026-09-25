import { useState } from 'react';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { Link } from 'react-router';
import { Search, Plus, MoreHorizontal, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { listMyGroupsApiV1GroupsGet } from '@/api/generated/sdk.gen';
import { totalCountFrom } from '@/api/total-count';
import {
  listGroupsApiV1AdminGroupsGetOptions,
  listGroupsApiV1AdminGroupsGetQueryKey,
  listMyGroupsApiV1GroupsGetQueryKey,
  createGroupApiV1AdminGroupsPostMutation,
  deleteGroupApiV1AdminGroupsGroupIdDeleteMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { TableRowsSkeleton } from '@/components/skeletons';
import { TableEmptyRow } from '@/components/EmptyState';
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
import { Pagination } from '@/components/admin/Pagination';
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';

export function GroupsPage() {
  const queryClient = useQueryClient();
  const { isAdmin, adminMode } = useAuth();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [newGroupName, setNewGroupName] = useState('');
  const [newGroupDescription, setNewGroupDescription] = useState('');
  const [deleteDialog, setDeleteDialog] = useState<{
    open: boolean;
    groupId: number;
    groupName: string;
  } | null>(null);

  const limit = 20;
  const isAdminView = isAdmin && adminMode;

  const debouncedSearch = useDebouncedValue(search);

  // Admins use the admin endpoint, group managers the my-groups one; both page
  // and search on the server.
  const { data: adminGroupsData, isLoading: isLoadingAdmin, isFetching: isFetchingAdmin } = useQuery({
    ...listGroupsApiV1AdminGroupsGetOptions({
      query: {
        page,
        limit,
        search: debouncedSearch || undefined,
      },
    }),
    enabled: isAdminView,
    placeholderData: keepPreviousData,
  });

  // The my-groups body is a bare array (agents read it too), so the count comes
  // from `X-Total-Count` — which the tanstack wrapper drops, hence the direct
  // operation call. The trailing key element keeps this `{rows, total}` shape
  // apart from the plain-array cache entries of other callers, while `_id`-
  // and prefix-based invalidations still reach it.
  const myGroupsQuery = { page, limit, search: debouncedSearch || undefined };
  const { data: myGroupsData, isLoading: isLoadingMy, isFetching: isFetchingMy } = useQuery({
    queryKey: [...listMyGroupsApiV1GroupsGetQueryKey({ query: myGroupsQuery }), 'with-total'] as const,
    queryFn: async ({ signal }) => {
      const { data, response } = await listMyGroupsApiV1GroupsGet({
        query: myGroupsQuery,
        signal,
        throwOnError: true,
      });
      return { rows: data, total: totalCountFrom(response, data.length) };
    },
    enabled: !isAdminView,
    placeholderData: keepPreviousData,
  });

  const isLoading = isAdminView ? isLoadingAdmin : isLoadingMy;
  const isFetching = isAdminView ? isFetchingAdmin : isFetchingMy;

  const groups = isAdminView ? (adminGroupsData?.data ?? []) : (myGroupsData?.rows ?? []);
  const meta = isAdminView
    ? (adminGroupsData?.meta ?? { page: 1, limit, total: 0 })
    : { page, limit, total: myGroupsData?.total ?? 0 };

  const createMutation = useMutation({
    ...createGroupApiV1AdminGroupsPostMutation(),
    onSuccess: () => {
      toast.success('Group created successfully');
      setCreateDialogOpen(false);
      setNewGroupName('');
      setNewGroupDescription('');
      // No query in the key: partial matching then refreshes every page and term.
      queryClient.invalidateQueries({ queryKey: listGroupsApiV1AdminGroupsGetQueryKey() });
    },
    onError: (error: any) => {
      const message = error?.detail || error?.response?.data?.detail || 'Failed to create group';
      toast.error(message);
    },
  });

  const deleteMutation = useMutation({
    ...deleteGroupApiV1AdminGroupsGroupIdDeleteMutation(),
    onSuccess: () => {
      toast.success('Group deleted successfully');
      // No query in the key: partial matching then refreshes every page and term.
      queryClient.invalidateQueries({ queryKey: listGroupsApiV1AdminGroupsGetQueryKey() });
    },
    onError: () => {
      toast.error('Failed to delete group');
    },
  });

  const handleCreateGroup = () => {
    if (!newGroupName.trim()) return;
    createMutation.mutate({
      body: {
        name: newGroupName.trim(),
        description: newGroupDescription.trim() || undefined,
      },
    });
  };

  const handleDeleteGroup = () => {
    if (!deleteDialog) return;
    deleteMutation.mutate({
      path: { group_id: deleteDialog.groupId },
      query: { force: true },
    });
    setDeleteDialog(null);
  };

  return (
    <div className="space-y-6 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Groups</h1>
          <p className="text-muted-foreground">
            {isAdminView ? 'Manage user groups and permissions' : 'Manage your groups'}
          </p>
        </div>
        {isAdminView && (
          <Button onClick={() => setCreateDialogOpen(true)}>
            <Plus className="h-4 w-4 mr-2" />
            Create Group
          </Button>
        )}
      </div>

      <div className="flex items-center gap-4">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search groups..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            className="pl-9"
          />
        </div>
      </div>

      <div className="border rounded-lg">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Description</TableHead>
              <TableHead>Members</TableHead>
              <TableHead>Created</TableHead>
              <TableHead className="w-12"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody className={isFetching && !isLoading ? 'opacity-60 transition-opacity' : 'transition-opacity'}>
            {isLoading ? (
              <TableRowsSkeleton columns={5} />
            ) : groups.length === 0 ? (
              <TableEmptyRow
                colSpan={5}
                title={debouncedSearch ? 'No groups match your search' : 'No groups found'}
              />
            ) : (
              groups.map((group) => (
                <TableRow key={group.id}>
                  <TableCell>
                    <Link
                      to={isAdminView ? `/app/admin/groups/${group.id}` : `/app/groups/${group.id}`}
                      className="font-medium hover:underline"
                    >
                      {group.name}
                    </Link>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {group.description || '-'}
                  </TableCell>
                  <TableCell>{group.member_count ?? group.members?.length ?? 0}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {group.created_at
                      ? new Date(group.created_at).toLocaleDateString()
                      : '-'}
                  </TableCell>
                  <TableCell>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon">
                          <MoreHorizontal className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem asChild>
                          <Link to={isAdminView ? `/app/admin/groups/${group.id}` : `/app/groups/${group.id}`}>
                            View Details
                          </Link>
                        </DropdownMenuItem>
                        {isAdminView && (
                          <DropdownMenuItem
                            className="text-destructive"
                            onClick={() =>
                              setDeleteDialog({
                                open: true,
                                groupId: group.id,
                                groupName: group.name,
                              })
                            }
                          >
                            <Trash2 className="h-4 w-4 mr-2" />
                            Delete
                          </DropdownMenuItem>
                        )}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <Pagination
        page={meta.page}
        limit={meta.limit}
        total={meta.total}
        onPageChange={setPage}
      />

      {/* Create Group Dialog - Admin only */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Group</DialogTitle>
            <DialogDescription>
              Create a new group to organize users and permissions.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                placeholder="Group name"
                value={newGroupName}
                onChange={(e) => setNewGroupName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="description">Description</Label>
              <Textarea
                id="description"
                placeholder="Optional description"
                value={newGroupDescription}
                onChange={(e) => setNewGroupDescription(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleCreateGroup}
              disabled={!newGroupName.trim() || createMutation.isPending}
            >
              {createMutation.isPending ? 'Creating...' : 'Create'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      {deleteDialog && (
        <ConfirmDialog
          open={deleteDialog.open}
          onOpenChange={(open) => !open && setDeleteDialog(null)}
          title="Delete Group"
          description={`Are you sure you want to delete "${deleteDialog.groupName}"? This action cannot be undone.`}
          confirmLabel="Delete"
          variant="destructive"
          onConfirm={handleDeleteGroup}
          isLoading={deleteMutation.isPending}
        />
      )}
    </div>
  );
}
