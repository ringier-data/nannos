import { useState } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, UserCheck, UserX, Trash2, Plus, X, UserCog } from 'lucide-react';
import { toast } from 'sonner';
import {
  getUserApiV1AdminUsersUserIdGetOptions,
  updateUserStatusApiV1AdminUsersUserIdStatusPutMutation,
  updateUserGroupsApiV1AdminUsersUserIdGroupsPutMutation,
  updateUserApiV1AdminUsersUserIdPatchMutation,
  updateUserRoleApiV1AdminUsersUserIdRolePutMutation,
  listGroupsApiV1AdminGroupsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { UserStatus, OperationEnum, UserRole } from '@/api/generated';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { UserStatusBadge } from '@/components/admin/UserStatusBadge';
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';
import { useAuth } from '@/contexts/AuthContext';

export function UserDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user: currentUser, adminMode, startImpersonation } = useAuth();
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    title: string;
    description: string;
    status: UserStatus;
  } | null>(null);
  const [selectedGroupToAdd, setSelectedGroupToAdd] = useState<string>('');

  const { data: userData, isLoading } = useQuery({
    ...getUserApiV1AdminUsersUserIdGetOptions({
      path: { user_id: id! },
    }),
    enabled: !!id,
  });

  const { data: allGroupsData } = useQuery({
    ...listGroupsApiV1AdminGroupsGetOptions({
      query: { limit: 100 },
    }),
  });

  const statusMutation = useMutation({
    ...updateUserStatusApiV1AdminUsersUserIdStatusPutMutation(),
    onSuccess: () => {
      toast.success('User status updated');
      queryClient.invalidateQueries({ queryKey: ['getUserApiV1AdminUsersUserIdGet'] });
      queryClient.invalidateQueries({ queryKey: ['listUsersApiV1AdminUsersGet'] });
    },
    onError: () => {
      toast.error('Failed to update user status');
    },
  });

  const groupsMutation = useMutation({
    ...updateUserGroupsApiV1AdminUsersUserIdGroupsPutMutation(),
    onSuccess: () => {
      toast.success('User groups updated');
      queryClient.invalidateQueries({ queryKey: ['getUserApiV1AdminUsersUserIdGet'] });
      queryClient.invalidateQueries({ queryKey: ['listUsersApiV1AdminUsersGet'] });
    },
    onError: () => {
      toast.error('Failed to update user groups');
    },
  });

  const adminUpdateMutation = useMutation({
    ...updateUserApiV1AdminUsersUserIdPatchMutation(),
    onSuccess: () => {
      toast.success('User updated');
      queryClient.invalidateQueries({ queryKey: ['getUserApiV1AdminUsersUserIdGet'] });
      queryClient.invalidateQueries({ queryKey: ['listUsersApiV1AdminUsersGet'] });
    },
    onError: () => {
      toast.error('Failed to update user');
    },
  });

  const roleMutation = useMutation({
    ...updateUserRoleApiV1AdminUsersUserIdRolePutMutation(),
    onSuccess: () => {
      toast.success('User role updated');
      queryClient.invalidateQueries({ 
        queryKey: getUserApiV1AdminUsersUserIdGetOptions({
          path: { user_id: id! },
        }).queryKey
      });
      queryClient.invalidateQueries({ queryKey: ['listUsersApiV1AdminUsersGet'] });
    },
    onError: () => {
      toast.error('Failed to update user role');
    },
  });

  const user = userData?.data;
  const allGroups = allGroupsData?.data ?? [];
  const userGroupIds = new Set(user?.groups?.map((g) => g.group_id) ?? []);
  const availableGroups = allGroups.filter((g) => !userGroupIds.has(g.id));
  const isViewingSelf = currentUser?.id === id;

  const handleStatusChange = (status: UserStatus) => {
    const statusLabels: Record<UserStatus, string> = {
      active: 'activate',
      suspended: 'suspend',
      deleted: 'delete',
    };
    setConfirmDialog({
      open: true,
      title: `${statusLabels[status].charAt(0).toUpperCase() + statusLabels[status].slice(1)} User`,
      description: `Are you sure you want to ${statusLabels[status]} this user?`,
      status,
    });
  };

  const executeStatusChange = () => {
    if (!confirmDialog || !id) return;
    statusMutation.mutate({
      path: { user_id: id },
      body: { status: confirmDialog.status },
    });
    setConfirmDialog(null);
  };

  const handleAddGroup = () => {
    if (!selectedGroupToAdd || !id) return;
    const groupId = parseInt(selectedGroupToAdd);
    groupsMutation.mutate({
      path: { user_id: id },
      body: {
        group_ids: [groupId],
        operation: 'add' as OperationEnum,
      },
    });
    setSelectedGroupToAdd('');
  };

  const handleRemoveGroup = (groupId: number) => {
    if (!id) return;
    groupsMutation.mutate({
      path: { user_id: id },
      body: {
        group_ids: [groupId],
        operation: 'remove' as OperationEnum,
      },
    });
  };

  const handleImpersonate = async () => {
    if (!adminMode) {
      toast.error('Admin mode must be enabled to impersonate users');
      return;
    }
    
    if (!user) return;
    
    try {
      await startImpersonation(user.id);
      toast.success(`Now impersonating ${user.email}`);
    } catch (error) {
      toast.error((error as Error).message || 'Failed to start impersonation');
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

  if (!user) {
    return (
      <div className="text-center py-12 p-4">
        <p className="text-muted-foreground">User not found</p>
        <Button variant="link" onClick={() => navigate('/app/admin/users')}>
          Back to Users
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6 p-4">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={() => navigate('/app/admin/users')}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="flex-1">
          <h1 className="text-2xl font-bold tracking-tight">
            {user.first_name} {user.last_name}
          </h1>
          <p className="text-muted-foreground">{user.email}</p>
        </div>
        <Button
          variant="outline"
          onClick={handleImpersonate}
          disabled={!adminMode || isViewingSelf}
        >
          <UserCog className="h-4 w-4 mr-2" />
          Impersonate User
        </Button>
        <UserStatusBadge status={user.status ?? 'active'} />
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>User Information</CardTitle>
            <CardDescription>Basic user details</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-sm text-muted-foreground">First Name</p>
                <p className="font-medium">{user.first_name}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Last Name</p>
                <p className="font-medium">{user.last_name}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Email</p>
                <p className="font-medium">{user.email}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Company</p>
                <p className="font-medium">{user.company_name || '-'}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Role</p>
                <Select
                  value={user.role ?? 'member'}
                  onValueChange={(value) => {
                    if (!id) return;
                    roleMutation.mutate({
                      path: { user_id: id },
                      body: { role: value as UserRole },
                    });
                  }}
                  disabled={roleMutation.isPending}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="member">Member</SelectItem>
                    <SelectItem value="approver">Approver</SelectItem>
                    <SelectItem value="admin">Admin</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Administrator</p>
                <div className="flex items-center gap-2 mt-1">
                  <Switch
                    id="is-admin"
                    checked={user.is_administrator ?? false}
                    onCheckedChange={(checked) => {
                      if (!id) return;
                      adminUpdateMutation.mutate({
                        path: { user_id: id },
                        body: { is_administrator: checked },
                      });
                    }}
                    disabled={isViewingSelf || adminUpdateMutation.isPending}
                  />
                  <Label htmlFor="is-admin" className="text-sm">
                    {user.is_administrator ? 'Yes' : 'No'}
                  </Label>
                  {isViewingSelf && (
                    <span className="text-xs text-muted-foreground">(cannot modify own status)</span>
                  )}
                </div>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Status Actions</CardTitle>
            <CardDescription>Manage user account status</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {user.status !== 'active' && (
                <Button
                  variant="outline"
                  onClick={() => handleStatusChange('active')}
                  disabled={statusMutation.isPending}
                >
                  <UserCheck className="h-4 w-4 mr-2" />
                  Activate
                </Button>
              )}
              {user.status === 'active' && (
                <Button
                  variant="outline"
                  onClick={() => handleStatusChange('suspended')}
                  disabled={statusMutation.isPending}
                >
                  <UserX className="h-4 w-4 mr-2" />
                  Suspend
                </Button>
              )}
              {user.status !== 'deleted' && (
                <Button
                  variant="destructive"
                  onClick={() => handleStatusChange('deleted')}
                  disabled={statusMutation.isPending}
                >
                  <Trash2 className="h-4 w-4 mr-2" />
                  Delete
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Group Memberships</CardTitle>
          <CardDescription>Manage user's group memberships</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Select value={selectedGroupToAdd} onValueChange={setSelectedGroupToAdd}>
              <SelectTrigger className="w-[300px]">
                <SelectValue placeholder="Select a group to add" />
              </SelectTrigger>
              <SelectContent>
                {availableGroups.length === 0 ? (
                  <div className="px-2 py-1.5 text-sm text-muted-foreground">
                    No available groups
                  </div>
                ) : (
                  availableGroups.map((group) => (
                    <SelectItem key={group.id} value={group.id.toString()}>
                      {group.name}
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
            <Button
              onClick={handleAddGroup}
              disabled={!selectedGroupToAdd || groupsMutation.isPending}
            >
              <Plus className="h-4 w-4 mr-1" />
              Add
            </Button>
          </div>

          <div className="flex flex-wrap gap-2">
            {user.groups?.length === 0 ? (
              <p className="text-sm text-muted-foreground">No group memberships</p>
            ) : (
              user.groups?.map((group) => (
                <Badge key={group.group_id} variant="secondary" className="gap-1">
                  {group.group_name}
                  <span className="text-xs text-muted-foreground">({group.group_role})</span>
                  <button
                    className="ml-1 hover:text-destructive"
                    onClick={() => handleRemoveGroup(group.group_id)}
                    disabled={groupsMutation.isPending}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))
            )}
          </div>
        </CardContent>
      </Card>

      {confirmDialog && (
        <ConfirmDialog
          open={confirmDialog.open}
          onOpenChange={(open) => !open && setConfirmDialog(null)}
          title={confirmDialog.title}
          description={confirmDialog.description}
          confirmLabel={confirmDialog.status === 'deleted' ? 'Delete' : 'Confirm'}
          variant={confirmDialog.status === 'deleted' ? 'destructive' : 'default'}
          onConfirm={executeStatusChange}
          isLoading={statusMutation.isPending}
        />
      )}
    </div>
  );
}
