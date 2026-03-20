import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Users, Loader2, Search, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
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
  getSecretPermissionsApiV1SecretsSecretIdPermissionsGetOptions,
  updateSecretPermissionsApiV1SecretsSecretIdPermissionsPutMutation,
  listMyGroupsApiV1GroupsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import { getErrorMessage } from '@/lib/utils';
import type { UserGroupWithMembers, SecretGroupPermission } from '@/api/generated/types.gen';
import { toast } from 'sonner';

interface SecretPermissionsDialogProps {
  secretId: number;
  secretName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface GroupPermissionState {
  groupId: number;
  role: 'none' | 'read' | 'write';
}

export function SecretPermissionsDialog({
  secretId,
  secretName,
  open,
  onOpenChange,
}: SecretPermissionsDialogProps) {
  const queryClient = useQueryClient();
  const [groupPermissions, setGroupPermissions] = useState<Map<number, GroupPermissionState>>(new Map());
  const [hasChanges, setHasChanges] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  // Fetch current permissions
  const { data: currentPermissions, isLoading: isLoadingPermissions } = useQuery({
    ...getSecretPermissionsApiV1SecretsSecretIdPermissionsGetOptions({
      path: { secret_id: secretId },
    }),
    enabled: open,
    retry: false,
  });

  // Fetch available groups (user's own groups)
  const { data: myGroupsData, isLoading: isLoadingGroups } = useQuery({
    ...listMyGroupsApiV1GroupsGetOptions(),
    enabled: open,
    retry: false,
  });

  const availableGroups: UserGroupWithMembers[] = myGroupsData ?? [];

  // Filter groups based on search
  const filteredGroups = availableGroups.filter(group => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      group.name.toLowerCase().includes(query) ||
      (group.description?.toLowerCase().includes(query) ?? false)
    );
  });

  // Sync state when permissions load
  useEffect(() => {
    if (currentPermissions) {
      const newMap = new Map<number, GroupPermissionState>();
      currentPermissions.forEach(perm => {
        const hasRead = perm.permissions.includes('read');
        const hasWrite = perm.permissions.includes('write');
        const role: 'none' | 'read' | 'write' = hasWrite ? 'write' : hasRead ? 'read' : 'none';
        newMap.set(perm.user_group_id, {
          groupId: perm.user_group_id,
          role,
        });
      });
      setGroupPermissions(newMap);
      setHasChanges(false);
    }
  }, [currentPermissions]);

  // Reset state when dialog closes
  useEffect(() => {
    if (!open) {
      setHasChanges(false);
      setSearchQuery('');
    }
  }, [open]);

  // Update mutation
  const updateMutation = useMutation({
    ...updateSecretPermissionsApiV1SecretsSecretIdPermissionsPutMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['getSecretPermissionsApiV1SecretsSecretIdPermissionsGet'],
      });
      setHasChanges(false);
      toast.success('Permissions updated successfully');
      onOpenChange(false);
    },
    onError: (err) => {
      toast.error('Failed to update permissions', { description: getErrorMessage(err) });
    },
  });

  const handleRoleChange = (groupId: number, role: 'none' | 'read' | 'write') => {
    setGroupPermissions(prev => {
      const newMap = new Map(prev);
      if (role === 'none') {
        newMap.delete(groupId);
      } else {
        newMap.set(groupId, { groupId, role });
      }
      return newMap;
    });
    setHasChanges(true);
  };

  const handleRemoveGroup = (groupId: number) => {
    handleRoleChange(groupId, 'none');
  };

  const handleSave = () => {
    const group_permissions: SecretGroupPermission[] = Array.from(groupPermissions.values())
      .filter(perm => perm.role !== 'none')
      .map(perm => ({
        user_group_id: perm.groupId,
        permissions: perm.role === 'write' ? ['read', 'write'] : ['read'],
      }));

    updateMutation.mutate({
      path: { secret_id: secretId },
      body: { group_permissions },
    });
  };

  // Get groups with permissions
  const groupsWithPermissions = filteredGroups
    .filter(group => groupPermissions.has(group.id))
    .map(group => ({
      ...group,
      permission: groupPermissions.get(group.id)!,
    }));

  // Get groups without permissions
  const groupsWithoutPermissions = filteredGroups.filter(group => !groupPermissions.has(group.id));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Manage Permissions - {secretName}
          </DialogTitle>
          <DialogDescription>
            Control which groups can access this secret. Groups with "read" can use it in sub-agents,
            "write" allows full management.
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-hidden flex flex-col gap-4 min-h-0">
          {/* Search */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search groups..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-9"
            />
          </div>

          {/* Loading state */}
          {(isLoadingPermissions || isLoadingGroups) && (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          )}

          {/* Groups with permissions */}
          {!isLoadingPermissions && !isLoadingGroups && (
            <div className="flex-1 overflow-y-auto min-h-0 space-y-6">
              {groupsWithPermissions.length > 0 && (
                <div className="space-y-2">
                  <h3 className="text-sm font-medium text-muted-foreground">Groups with Access</h3>
                  <div className="border rounded-lg overflow-hidden">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="w-[250px]">Group</TableHead>
                          <TableHead>Description</TableHead>
                          <TableHead className="w-[150px]">Permission</TableHead>
                          <TableHead className="w-[80px]"></TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {groupsWithPermissions.map(({ id, name, description, permission }) => (
                          <TableRow key={id}>
                            <TableCell className="font-medium">{name}</TableCell>
                            <TableCell className="text-muted-foreground">
                              {description || '-'}
                            </TableCell>
                            <TableCell>
                              <Select
                                value={permission.role}
                                onValueChange={(value) => handleRoleChange(id, value as 'read' | 'write')}
                              >
                                <SelectTrigger>
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectItem value="read">Read</SelectItem>
                                  <SelectItem value="write">Write</SelectItem>
                                </SelectContent>
                              </Select>
                            </TableCell>
                            <TableCell>
                              <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => handleRemoveGroup(id)}
                              >
                                <Trash2 className="h-4 w-4" />
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              )}

              {/* Groups without permissions */}
              {groupsWithoutPermissions.length > 0 && (
                <div className="space-y-2">
                  <h3 className="text-sm font-medium text-muted-foreground">Available Groups</h3>
                  <div className="border rounded-lg overflow-hidden">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="w-[250px]">Group</TableHead>
                          <TableHead>Description</TableHead>
                          <TableHead className="w-[150px]">Permission</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {groupsWithoutPermissions.map(({ id, name, description }) => (
                          <TableRow key={id}>
                            <TableCell className="font-medium">{name}</TableCell>
                            <TableCell className="text-muted-foreground">
                              {description || '-'}
                            </TableCell>
                            <TableCell>
                              <Select
                                value="none"
                                onValueChange={(value) => handleRoleChange(id, value as 'read' | 'write')}
                              >
                                <SelectTrigger>
                                  <SelectValue placeholder="None" />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectItem value="none">None</SelectItem>
                                  <SelectItem value="read">Read</SelectItem>
                                  <SelectItem value="write">Write</SelectItem>
                                </SelectContent>
                              </Select>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              )}

              {availableGroups.length === 0 && !isLoadingGroups && (
                <div className="text-center py-8 text-muted-foreground">
                  <Users className="h-12 w-12 mx-auto mb-2 opacity-20" />
                  <p>No groups available. Create groups to share this secret.</p>
                </div>
              )}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={!hasChanges || updateMutation.isPending}>
            {updateMutation.isPending && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
            Save Changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
