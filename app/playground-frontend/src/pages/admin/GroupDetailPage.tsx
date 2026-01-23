import { useState } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Plus, X, Save, UserPlus } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import {
  getGroupApiV1AdminGroupsGroupIdGetOptions,
  getGroupApiV1GroupsGroupIdGetOptions,
  updateGroupApiV1AdminGroupsGroupIdPutMutation,
  listMembersApiV1GroupsGroupIdMembersGetOptions,
  addMembersApiV1GroupsGroupIdMembersPostMutation,
  removeMembersApiV1GroupsGroupIdMembersRemovePostMutation,
  updateMemberRoleApiV1GroupsGroupIdMembersUserIdPutMutation,
  listUsersApiV1AdminUsersGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { RoleEnum } from '@/api/generated';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Checkbox } from '@/components/ui/checkbox';
import { Skeleton } from '@/components/ui/skeleton';
import { Pagination } from '@/components/admin/Pagination';

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

  const [selectedMembersToRemove, setSelectedMembersToRemove] = useState<Set<string>>(new Set());

  const [membersPage, setMembersPage] = useState(1);

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
      query: { page: membersPage, limit: 20 },
    }),
    enabled: !isNaN(groupId),
  });

  const { data: usersData } = useQuery({
    ...listUsersApiV1AdminUsersGetOptions({
      query: { limit: 100 },
    }),
    enabled: addMemberDialogOpen,
  });

  const updateMutation = useMutation({
    ...updateGroupApiV1AdminGroupsGroupIdPutMutation(),
    onSuccess: () => {
      toast.success('Group updated successfully');
      setIsEditing(false);
      queryClient.invalidateQueries({ 
        queryKey: getGroupApiV1AdminGroupsGroupIdGetOptions({
          path: { group_id: groupId },
        }).queryKey
      });
      queryClient.invalidateQueries({ 
        predicate: (query) => 
          query.queryKey[0] === 'listGroupsApiV1AdminGroupsGet'
      });
    },
    onError: (error: any) => {
      const message = error?.response?.data?.detail || 'Failed to update group';
      toast.error(message);
    },
  });

  const addMembersMutation = useMutation({
    ...addMembersApiV1GroupsGroupIdMembersPostMutation(),
    onSuccess: () => {
      toast.success('Members added successfully');
      setAddMemberDialogOpen(false);
      setSelectedUsersToAdd(new Set());
      queryClient.invalidateQueries({ 
        queryKey: listMembersApiV1GroupsGroupIdMembersGetOptions({
          path: { group_id: groupId },
          query: { page: membersPage, limit: 20 },
        }).queryKey
      });
      queryClient.invalidateQueries({ 
        queryKey: getGroupApiV1AdminGroupsGroupIdGetOptions({
          path: { group_id: groupId },
        }).queryKey
      });
    },
    onError: (error: any) => {
      const message = error?.response?.data?.detail || 'Failed to add members';
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
        }).queryKey
      });
      queryClient.invalidateQueries({ 
        queryKey: getGroupApiV1AdminGroupsGroupIdGetOptions({
          path: { group_id: groupId },
        }).queryKey
      });
    },
    onError: (error: any) => {
      const detail = error?.response?.data?.detail || error?.detail || error?.message;
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
        }).queryKey
      });
    },
    onError: (error: any) => {
      const message = error?.response?.data?.detail || 'Failed to update role';
      toast.error(message);
    },
  });

  const group = groupData?.data;
  const members = membersData?.data ?? [];
  const membersMeta = membersData?.meta ?? { page: 1, limit: 20, total: 0 };
  const allUsers = usersData?.data ?? [];
  const memberUserIds = new Set(members.map((m) => m.user_id));
  const availableUsers = allUsers.filter((u) => !memberUserIds.has(u.id));

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
        {!isEditing && isAdminView && (
          <Button onClick={startEditing}>Edit Group</Button>
        )}
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
                <Input
                  id="name"
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                />
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
                Permissions are managed through user system roles and group member roles.
                Members can have Read, Write, or Manager access to group resources.
              </p>
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
                onClick={handleRemoveSelectedMembers}
                disabled={removeMembersMutation.isPending}
              >
                <X className="h-4 w-4 mr-2" />
                {removeMembersMutation.isPending
                  ? 'Removing...'
                  : `Remove ${selectedMembersToRemove.size} Selected`}
              </Button>
            )}
            <Button onClick={() => setAddMemberDialogOpen(true)}>
              <UserPlus className="h-4 w-4 mr-2" />
              Add Members
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="border rounded-lg">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12">
                    <Checkbox
                      checked={members.length > 0 && selectedMembersToRemove.size === members.length}
                      onCheckedChange={(checked) => {
                        if (checked) {
                          setSelectedMembersToRemove(new Set(members.map(m => m.user_id)));
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
                      No members
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
                          onValueChange={(value) =>
                            handleRoleChange(member.user_id, value as RoleEnum)
                          }
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
      <Dialog open={addMemberDialogOpen} onOpenChange={setAddMemberDialogOpen}>
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
              <div className="border rounded-lg max-h-64 overflow-y-auto">
                {availableUsers.length === 0 ? (
                  <div className="p-4 text-center text-muted-foreground">
                    No available users to add
                  </div>
                ) : (
                  availableUsers.map((user) => (
                    <div
                      key={user.id}
                      className="flex items-center gap-3 p-3 border-b last:border-b-0"
                    >
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
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddMemberDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleAddMembers}
              disabled={selectedUsersToAdd.size === 0 || addMembersMutation.isPending}
            >
              <Plus className="h-4 w-4 mr-1" />
              {addMembersMutation.isPending ? 'Adding...' : `Add ${selectedUsersToAdd.size} User(s)`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
