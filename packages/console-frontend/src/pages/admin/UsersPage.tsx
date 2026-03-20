import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router';
import { Search, MoreHorizontal, UserCheck, UserX, Trash2, UserCog, Eye } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import {
  listUsersApiV1AdminUsersGetOptions,
  bulkUpdateUsersApiV1AdminUsersBulkPostMutation,
  listGroupsApiV1AdminGroupsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { UserWithGroups, ActionEnum } from '@/api/generated';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Checkbox } from '@/components/ui/checkbox';
import { Pagination } from '@/components/admin/Pagination';
import { UserStatusBadge } from '@/components/admin/UserStatusBadge';
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';

export function UsersPage() {
  const queryClient = useQueryClient();
  const { adminMode, startImpersonation } = useAuth();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [groupFilter, setGroupFilter] = useState<string>('all');
  const [selectedUsers, setSelectedUsers] = useState<Set<string>>(new Set());
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    title: string;
    description: string;
    action: ActionEnum;
    userIds: string[];
  } | null>(null);

  const limit = 20;

  const { data: usersData, isLoading } = useQuery({
    ...listUsersApiV1AdminUsersGetOptions({
      query: {
        page,
        limit,
        search: search || undefined,
        group_id: groupFilter !== 'all' ? parseInt(groupFilter) : undefined,
      },
    }),
  });

  const { data: groupsData } = useQuery({
    ...listGroupsApiV1AdminGroupsGetOptions({
      query: { limit: 100 },
    }),
  });

  const bulkMutation = useMutation({
    ...bulkUpdateUsersApiV1AdminUsersBulkPostMutation(),
    onSuccess: (data) => {
      const successCount = data.data.filter((r) => r.success).length;
      const failCount = data.data.filter((r) => !r.success).length;
      if (failCount === 0) {
        toast.success(`Successfully updated ${successCount} user(s)`);
      } else {
        toast.warning(`Updated ${successCount} user(s), ${failCount} failed`);
      }
      setSelectedUsers(new Set());
      queryClient.invalidateQueries({ queryKey: ['listUsersApiV1AdminUsersGet'] });
    },
    onError: () => {
      toast.error('Failed to update users');
    },
  });

  const users = usersData?.data ?? [];
  const meta = usersData?.meta ?? { page: 1, limit: 20, total: 0 };
  const groups = groupsData?.data ?? [];

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedUsers(new Set(users.map((u) => u.id)));
    } else {
      setSelectedUsers(new Set());
    }
  };

  const handleSelectUser = (userId: string, checked: boolean) => {
    const newSelected = new Set(selectedUsers);
    if (checked) {
      newSelected.add(userId);
    } else {
      newSelected.delete(userId);
    }
    setSelectedUsers(newSelected);
  };

  const handleBulkAction = (action: ActionEnum) => {
    const userIds = Array.from(selectedUsers);
    const actionLabels: Record<ActionEnum, string> = {
      activate: 'activate',
      suspend: 'suspend',
      delete: 'delete',
    };
    setConfirmDialog({
      open: true,
      title: `${actionLabels[action].charAt(0).toUpperCase() + actionLabels[action].slice(1)} Users`,
      description: `Are you sure you want to ${actionLabels[action]} ${userIds.length} user(s)?`,
      action,
      userIds,
    });
  };

  const handleSingleAction = (user: UserWithGroups, action: ActionEnum) => {
    const actionLabels: Record<ActionEnum, string> = {
      activate: 'activate',
      suspend: 'suspend',
      delete: 'delete',
    };
    setConfirmDialog({
      open: true,
      title: `${actionLabels[action].charAt(0).toUpperCase() + actionLabels[action].slice(1)} User`,
      description: `Are you sure you want to ${actionLabels[action]} ${user.email}?`,
      action,
      userIds: [user.id],
    });
  };

  const executeAction = () => {
    if (!confirmDialog) return;
    bulkMutation.mutate({
      body: {
        operations: confirmDialog.userIds.map((user_id) => ({
          user_id,
          action: confirmDialog.action,
        })),
      },
    });
    setConfirmDialog(null);
  };

  const allSelected = users.length > 0 && users.every((u) => selectedUsers.has(u.id));
  const someSelected = selectedUsers.size > 0;

  const handleImpersonate = async (user: UserWithGroups) => {
    if (!adminMode) {
      toast.error('Admin mode must be enabled to impersonate users');
      return;
    }
    
    try {
      await startImpersonation(user.id);
      toast.success(`Now impersonating ${user.email}`);
    } catch (error) {
      toast.error((error as Error).message || 'Failed to start impersonation');
    }
  };

  return (
    <div className="space-y-6 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Users</h1>
          <p className="text-muted-foreground">Manage user accounts and permissions</p>
        </div>
      </div>

      <div className="flex items-center gap-4">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search by name or email..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            className="pl-9"
          />
        </div>
        <Select
          value={groupFilter}
          onValueChange={(value) => {
            setGroupFilter(value);
            setPage(1);
          }}
        >
          <SelectTrigger className="w-[200px]">
            <SelectValue placeholder="Filter by group" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Groups</SelectItem>
            {groups.map((group) => (
              <SelectItem key={group.id} value={group.id.toString()}>
                {group.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {someSelected && (
        <div className="flex items-center gap-2 p-3 bg-muted rounded-lg">
          <span className="text-sm font-medium">{selectedUsers.size} selected</span>
          <div className="flex-1" />
          <Button
            variant="outline"
            size="sm"
            onClick={() => handleBulkAction('activate')}
          >
            <UserCheck className="h-4 w-4 mr-1" />
            Activate
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => handleBulkAction('suspend')}
          >
            <UserX className="h-4 w-4 mr-1" />
            Suspend
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={() => handleBulkAction('delete')}
          >
            <Trash2 className="h-4 w-4 mr-1" />
            Delete
          </Button>
        </div>
      )}

      <div className="border rounded-lg">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12">
                <Checkbox
                  checked={allSelected}
                  onCheckedChange={handleSelectAll}
                />
              </TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Email</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Groups</TableHead>
              <TableHead className="w-12"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8">
                  Loading...
                </TableCell>
              </TableRow>
            ) : users.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8 text-muted-foreground">
                  No users found
                </TableCell>
              </TableRow>
            ) : (
              users.map((user) => (
                <TableRow key={user.id}>
                  <TableCell>
                    <Checkbox
                      checked={selectedUsers.has(user.id)}
                      onCheckedChange={(checked) => handleSelectUser(user.id, checked as boolean)}
                    />
                  </TableCell>
                  <TableCell>
                    <Link
                      to={`/app/admin/users/${user.id}`}
                      className="font-medium hover:underline"
                    >
                      {user.first_name} {user.last_name}
                    </Link>
                    {user.is_administrator && (
                      <span className="ml-2 text-xs text-muted-foreground">(Admin)</span>
                    )}
                  </TableCell>
                  <TableCell>{user.email}</TableCell>
                  <TableCell>
                    <UserStatusBadge status={user.status ?? 'active'} />
                  </TableCell>
                  <TableCell>
                    <span className="text-sm capitalize">{user.role ?? 'member'}</span>
                  </TableCell>
                  <TableCell>
                    <span className="text-sm text-muted-foreground">
                      {user.groups?.length ?? 0} group(s)
                    </span>
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
                          <Link to={`/app/admin/users/${user.id}`}>
                            <Eye className="h-4 w-4 mr-2" />
                            View Details
                          </Link>
                        </DropdownMenuItem>
                        <DropdownMenuItem 
                          onClick={() => handleImpersonate(user)}
                          disabled={!adminMode}
                        >
                          <UserCog className="h-4 w-4 mr-2" />
                          Impersonate User
                        </DropdownMenuItem>
                        {user.status !== 'active' && (
                          <DropdownMenuItem onClick={() => handleSingleAction(user, 'activate')}>
                            <UserCheck className="h-4 w-4 mr-2" />
                            Activate
                          </DropdownMenuItem>
                        )}
                        {user.status === 'active' && (
                          <DropdownMenuItem onClick={() => handleSingleAction(user, 'suspend')}>
                            <UserX className="h-4 w-4 mr-2" />
                            Suspend
                          </DropdownMenuItem>
                        )}
                        {user.status !== 'deleted' && (
                          <DropdownMenuItem
                            className="text-destructive"
                            onClick={() => handleSingleAction(user, 'delete')}
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

      {confirmDialog && (
        <ConfirmDialog
          open={confirmDialog.open}
          onOpenChange={(open) => !open && setConfirmDialog(null)}
          title={confirmDialog.title}
          description={confirmDialog.description}
          confirmLabel={confirmDialog.action === 'delete' ? 'Delete' : 'Confirm'}
          variant={confirmDialog.action === 'delete' ? 'destructive' : 'default'}
          onConfirm={executeAction}
          isLoading={bulkMutation.isPending}
        />
      )}
    </div>
  );
}
