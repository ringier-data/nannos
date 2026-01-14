import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Users, Loader2, Search, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
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
  getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetOptions,
  updateSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsPutMutation,
  listMyGroupsApiV1GroupsGetOptions,
  listGroupsApiV1AdminGroupsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import { getErrorMessage } from '@/lib/utils';
import type { UserGroupWithMembers, SubAgentGroupPermission } from '@/api/generated/types.gen';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';

interface SubAgentPermissionsDialogProps {
  subAgentId: number;
  subAgentName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface GroupPermissionState {
  groupId: number;
  role: 'none' | 'read' | 'write';
}

export function SubAgentPermissionsDialog({
  subAgentId,
  subAgentName,
  open,
  onOpenChange,
}: SubAgentPermissionsDialogProps) {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const isAdmin = user?.is_administrator ?? false;
  const [groupPermissions, setGroupPermissions] = useState<Map<number, GroupPermissionState>>(new Map());
  const [selectedGroups, setSelectedGroups] = useState<Set<number>>(new Set());
  const [hasChanges, setHasChanges] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  // Fetch current permissions
  const { data: currentPermissions, isLoading: isLoadingPermissions, error: permissionsError } = useQuery({
    ...getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetOptions({
      path: { sub_agent_id: subAgentId },
    }),
    enabled: open,
    retry: false,
  });

  // Fetch available groups - admins see all groups, owners see only their groups
  const { data: myGroupsData, isLoading: isLoadingMyGroups, error: myGroupsError } = useQuery({
    ...listMyGroupsApiV1GroupsGetOptions(),
    enabled: open && !isAdmin,
    retry: false,
  });

  const { data: allGroupsData, isLoading: isLoadingAllGroups, error: allGroupsError } = useQuery({
    ...listGroupsApiV1AdminGroupsGetOptions(),
    enabled: open && isAdmin,
    retry: false,
  });

  const availableGroups: UserGroupWithMembers[] = isAdmin
    ? allGroupsData?.data ?? []
    : myGroupsData ?? [];

  const isLoadingGroups = isAdmin ? isLoadingAllGroups : isLoadingMyGroups;

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
      setSelectedGroups(new Set());
      setSearchQuery('');
    }
  }, [open]);

  // Update mutation
  const updateMutation = useMutation({
    ...updateSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsPutMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGet'],
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
      setHasChanges(true);
      return newMap;
    });
  };

  const toggleSelectGroup = (groupId: number) => {
    setSelectedGroups(prev => {
      const newSet = new Set(prev);
      if (newSet.has(groupId)) {
        newSet.delete(groupId);
      } else {
        newSet.add(groupId);
      }
      return newSet;
    });
  };

  const toggleSelectAll = () => {
    if (selectedGroups.size === filteredGroups.filter(g => groupPermissions.has(g.id)).length) {
      setSelectedGroups(new Set());
    } else {
      const allGroupIds = filteredGroups.filter(g => groupPermissions.has(g.id)).map(g => g.id);
      setSelectedGroups(new Set(allGroupIds));
    }
  };

  const handleBulkDelete = () => {
    setGroupPermissions(prev => {
      const newMap = new Map(prev);
      selectedGroups.forEach(id => newMap.delete(id));
      return newMap;
    });
    setSelectedGroups(new Set());
    setHasChanges(true);
  };

  const handleBulkRoleChange = (role: 'read' | 'write') => {
    setGroupPermissions(prev => {
      const newMap = new Map(prev);
      selectedGroups.forEach(id => {
        newMap.set(id, { groupId: id, role });
      });
      return newMap;
    });
    setHasChanges(true);
  };

  const handleSave = () => {
    const group_permissions: SubAgentGroupPermission[] = Array.from(groupPermissions.values())
      .map(gp => {
        const permissions: Array<'read' | 'write'> = [];
        if (gp.role === 'read') {
          permissions.push('read');
        } else if (gp.role === 'write') {
          permissions.push('read', 'write');
        }
        return {
          user_group_id: gp.groupId,
          permissions,
        };
      })
      .filter(gp => gp.permissions.length > 0);

    updateMutation.mutate({
      path: { sub_agent_id: subAgentId },
      body: { group_permissions },
    });
  };

  const isLoading = isLoadingPermissions || isLoadingGroups;
  const hasError = permissionsError || (isAdmin ? allGroupsError : myGroupsError);

  // Only show groups with permissions, or groups matching search (to add new ones)
  const groupsWithPermissions = availableGroups.filter(g => groupPermissions.has(g.id));
  const searchResults = searchQuery.trim() 
    ? filteredGroups.filter(g => !groupPermissions.has(g.id)) 
    : [];
  
  const displayGroups = searchQuery.trim() 
    ? [...groupsWithPermissions, ...searchResults].sort((a, b) => {
        const aHasPerm = groupPermissions.has(a.id);
        const bHasPerm = groupPermissions.has(b.id);
        if (aHasPerm === bHasPerm) return a.name.localeCompare(b.name);
        return aHasPerm ? -1 : 1; // Groups with permissions first
      })
    : groupsWithPermissions;

  const allSelectedInView = selectedGroups.size > 0 && groupsWithPermissions.every(g => selectedGroups.has(g.id));
  const someSelectedInView = selectedGroups.size > 0 && groupsWithPermissions.some(g => selectedGroups.has(g.id)) && !allSelectedInView;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Manage Group Permissions
          </DialogTitle>
          <DialogDescription>
            Assign read and write permissions to groups for "{subAgentName}".
            <br />
            <span className="text-xs">
              <strong>Read:</strong> Can activate | <strong>Write:</strong> Can edit and manage permissions
            </span>
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 flex flex-col min-h-0 space-y-4">
          {isLoading && (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading groups...
            </div>
          )}

          {hasError && (
            <div className="py-8 text-center">
              <p className="text-sm text-destructive mb-2">
                {getErrorMessage(hasError)}
              </p>
              <p className="text-xs text-muted-foreground">
                {permissionsError 
                  ? "You don't have permission to manage access for this sub-agent."
                  : "Failed to load available groups."}
              </p>
            </div>
          )}

          {!isLoading && !hasError && availableGroups.length === 0 && (
            <div className="py-8 text-center text-sm text-muted-foreground">
              No groups available.
              {!isAdmin && ' You must be a member of a group to share access.'}
            </div>
          )}

          {!isLoading && !hasError && availableGroups.length > 0 && (
            <>
              {/* Search and Bulk Actions */}
              <div className="flex items-center gap-2">
                <div className="relative flex-1">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search groups to add..."
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="pl-9"
                  />
                </div>
                {selectedGroups.size > 0 && (
                  <div className="flex items-center gap-2">
                    <Select onValueChange={(value) => handleBulkRoleChange(value as 'read' | 'write')}>
                      <SelectTrigger className="w-[140px]">
                        <SelectValue placeholder="Set role..." />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="read">Read</SelectItem>
                        <SelectItem value="write">Write</SelectItem>
                      </SelectContent>
                    </Select>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleBulkDelete}
                    >
                      <Trash2 className="h-4 w-4 mr-2" />
                      Remove ({selectedGroups.size})
                    </Button>
                  </div>
                )}
              </div>

              {groupsWithPermissions.length === 0 && !searchQuery.trim() && (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  No groups have access yet. Search above to add groups.
                </div>
              )}

              {/* Table */}
              {(groupsWithPermissions.length > 0 || searchQuery.trim()) && (
                <div className="border rounded-md flex-1 overflow-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead className="w-12">
                          <Checkbox
                            checked={allSelectedInView}
                            ref={(el) => {
                              if (el) {
                                (el as any).indeterminate = someSelectedInView;
                              }
                            }}
                            onCheckedChange={toggleSelectAll}
                            aria-label="Select all"
                          />
                        </TableHead>
                        <TableHead>Group</TableHead>
                        <TableHead className="w-[180px]">Role</TableHead>
                        <TableHead className="w-12"></TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {displayGroups.length === 0 ? (
                        <TableRow>
                          <TableCell colSpan={4} className="text-center py-8 text-muted-foreground">
                            No groups match your search.
                          </TableCell>
                        </TableRow>
                      ) : (
                        displayGroups.map((group) => {
                        const permission = groupPermissions.get(group.id);
                        const isSelected = selectedGroups.has(group.id);
                        const role = permission?.role || 'none';
                        const memberCount = group.members?.length || 0;

                        return (
                          <TableRow key={group.id} className={isSelected ? 'bg-muted/50' : undefined}>
                            <TableCell>
                              <Checkbox
                                checked={isSelected}
                                onCheckedChange={() => toggleSelectGroup(group.id)}
                                disabled={!permission}
                                aria-label={`Select ${group.name}`}
                              />
                            </TableCell>
                            <TableCell>
                              <div>
                                <div className="font-medium">{group.name}</div>
                                <div className="text-xs text-muted-foreground">
                                  {memberCount} {memberCount === 1 ? 'member' : 'members'}
                                  {group.description && ` • ${group.description}`}
                                </div>
                              </div>
                            </TableCell>
                            <TableCell>
                              <Select
                                value={role}
                                onValueChange={(value) => handleRoleChange(group.id, value as 'none' | 'read' | 'write')}
                              >
                                <SelectTrigger className="w-[160px]">
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectItem value="none">No access</SelectItem>
                                  <SelectItem value="read">Read</SelectItem>
                                  <SelectItem value="write">Write</SelectItem>
                                </SelectContent>
                              </Select>
                            </TableCell>
                            <TableCell>
                              {permission && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-8 w-8"
                                  onClick={() => handleRoleChange(group.id, 'none')}
                                >
                                  <Trash2 className="h-4 w-4" />
                                </Button>
                              )}
                            </TableCell>
                          </TableRow>
                        );
                      })
                    )}
                  </TableBody>
                </Table>
              </div>
              )}
            </>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            disabled={!hasChanges || updateMutation.isPending || !!hasError}
          >
            {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
