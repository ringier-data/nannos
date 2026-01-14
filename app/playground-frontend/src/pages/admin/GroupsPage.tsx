import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router';
import { Search, Plus, MoreHorizontal, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import {
  listGroupsApiV1AdminGroupsGetOptions,
  listMyGroupsApiV1GroupsGetOptions,
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

  // Admins use the admin endpoint with pagination, group managers use the my groups endpoint
  const { data: adminGroupsData, isLoading: isLoadingAdmin } = useQuery({
    ...listGroupsApiV1AdminGroupsGetOptions({
      query: {
        page,
        limit,
        search: search || undefined,
      },
    }),
    enabled: isAdminView,
  });

  const { data: myGroupsData, isLoading: isLoadingMy } = useQuery({
    ...listMyGroupsApiV1GroupsGetOptions(),
    enabled: !isAdminView,
  });

  const isLoading = isAdminView ? isLoadingAdmin : isLoadingMy;

  // For group managers, filter locally by search
  const filteredMyGroups = myGroupsData?.filter(group => {
    if (!search) return true;
    const query = search.toLowerCase();
    return group.name.toLowerCase().includes(query) || 
           (group.description?.toLowerCase().includes(query) ?? false);
  }) ?? [];

  const groups = isAdminView ? (adminGroupsData?.data ?? []) : filteredMyGroups;
  const meta = isAdminView 
    ? (adminGroupsData?.meta ?? { page: 1, limit: 20, total: 0 })
    : { page: 1, limit: filteredMyGroups.length, total: filteredMyGroups.length };

  const createMutation = useMutation({
    ...createGroupApiV1AdminGroupsPostMutation(),
    onSuccess: () => {
      toast.success('Group created successfully');
      setCreateDialogOpen(false);
      setNewGroupName('');
      setNewGroupDescription('');
      queryClient.invalidateQueries({ 
        queryKey: listGroupsApiV1AdminGroupsGetOptions({
          query: {
            page,
            limit,
            search: search || undefined,
          },
        }).queryKey
      });
    },
    onError: () => {
      toast.error('Failed to create group');
    },
  });

  const deleteMutation = useMutation({
    ...deleteGroupApiV1AdminGroupsGroupIdDeleteMutation(),
    onSuccess: () => {
      toast.success('Group deleted successfully');
      queryClient.invalidateQueries({ 
        queryKey: listGroupsApiV1AdminGroupsGetOptions({
          query: {
            page,
            limit,
            search: search || undefined,
          },
        }).queryKey
      });
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
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center py-8">
                  Loading...
                </TableCell>
              </TableRow>
            ) : groups.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">
                  No groups found
                </TableCell>
              </TableRow>
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

      {isAdminView && (
        <Pagination
          page={meta.page}
          limit={meta.limit}
          total={meta.total}
          onPageChange={setPage}
        />
      )}

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
