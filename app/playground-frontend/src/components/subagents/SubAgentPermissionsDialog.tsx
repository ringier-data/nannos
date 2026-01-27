import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Users, Loader2, Search, HelpCircle } from 'lucide-react';
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
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetOptions,
  updateSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsPutMutation,
  listMyGroupsApiV1GroupsGetOptions,
  getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGetOptions,
  addGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdPostMutation,
  removeGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdDeleteMutation,
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
  const [defaultGroups, setDefaultGroups] = useState<Set<number>>(new Set());
  const [initialDefaults, setInitialDefaults] = useState<Set<number>>(new Set());
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

  // Fetch available groups - use regular groups endpoint (shows groups where user is a member)
  const { data: groupsData, isLoading: isLoadingGroups, error: groupsError } = useQuery({
    ...listMyGroupsApiV1GroupsGetOptions(),
    enabled: open,
    retry: false,
  });

  const availableGroups: UserGroupWithMembers[] = groupsData ?? [];

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

  // Fetch default agents for all groups that have permissions
  useEffect(() => {
    if (!open || !availableGroups.length) return;

    const fetchDefaults = async () => {
      const defaults = new Set<number>();
      
      for (const group of availableGroups) {
        try {
          const result = await queryClient.fetchQuery(
            getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGetOptions({
              path: { group_id: group.id },
            })
          );
          
          if (result && Array.isArray(result)) {
            const hasThisAgent = result.some((agent: any) => agent.id === subAgentId);
            if (hasThisAgent) {
              defaults.add(group.id);
            }
          }
        } catch (err) {
          // Ignore errors for individual groups
          console.warn(`Failed to fetch defaults for group ${group.id}:`, err);
        }
      }
      
      setDefaultGroups(defaults);
      setInitialDefaults(defaults);
    };

    fetchDefaults();
  }, [open, availableGroups, subAgentId, queryClient]);

  // Reset state when dialog closes
  useEffect(() => {
    if (!open) {
      setHasChanges(false);
      setSearchQuery('');
      setDefaultGroups(new Set());
      setInitialDefaults(new Set());
    }
  }, [open]);

  // Track changes in permissions or defaults
  useEffect(() => {
    const defaultsChanged = 
      defaultGroups.size !== initialDefaults.size ||
      Array.from(defaultGroups).some(id => !initialDefaults.has(id)) ||
      Array.from(initialDefaults).some(id => !defaultGroups.has(id));
    
    setHasChanges(defaultsChanged);
  }, [defaultGroups, initialDefaults]);

  // Update mutation
  const updateMutation = useMutation({
    ...updateSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsPutMutation(),
    onError: (err) => {
      toast.error('Failed to update permissions', { description: getErrorMessage(err) });
    },
  });

  const addDefaultMutation = useMutation({
    ...addGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdPostMutation(),
  });

  const removeDefaultMutation = useMutation({
    ...removeGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdDeleteMutation(),
  });

  const handleRoleChange = (groupId: number, role: 'none' | 'read' | 'write') => {
    setGroupPermissions(prev => {
      const newMap = new Map(prev);
      const wasNone = !prev.has(groupId);
      
      if (role === 'none') {
        newMap.delete(groupId);
        // Remove from defaults when removing permissions
        setDefaultGroups(defaults => {
          const newDefaults = new Set(defaults);
          newDefaults.delete(groupId);
          return newDefaults;
        });
      } else {
        newMap.set(groupId, { groupId, role });
        // Auto-enable default when granting permissions for the first time
        if (wasNone) {
          setDefaultGroups(defaults => {
            const newDefaults = new Set(defaults);
            newDefaults.add(groupId);
            return newDefaults;
          });
        }
      }
      setHasChanges(true);
      return newMap;
    });
  };

  const handleSave = async () => {
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

    try {
      // Update permissions
      await updateMutation.mutateAsync({
        path: { sub_agent_id: subAgentId },
        body: { group_permissions },
      });

      // Handle default status changes
      const toAdd = Array.from(defaultGroups).filter(id => !initialDefaults.has(id));
      const toRemove = Array.from(initialDefaults).filter(id => !defaultGroups.has(id));

      const defaultPromises = [
        ...toAdd.map(groupId => 
          addDefaultMutation.mutateAsync({
            path: { group_id: groupId, sub_agent_id: subAgentId },
          })
        ),
        ...toRemove.map(groupId =>
          removeDefaultMutation.mutateAsync({
            path: { group_id: groupId, sub_agent_id: subAgentId },
          })
        ),
      ];

      if (defaultPromises.length > 0) {
        await Promise.all(defaultPromises);
      }

      toast.success('Permissions and defaults updated successfully');
      onOpenChange(false);
    } catch (err) {
      // Error already handled by mutation onError
    }
  };

  const isLoading = isLoadingPermissions || isLoadingGroups;
  const hasError = permissionsError || groupsError;

  // Show ALL available groups, filtered by search query
  const displayGroups = filteredGroups.sort((a, b) => {
    const aHasPerm = groupPermissions.has(a.id);
    const bHasPerm = groupPermissions.has(b.id);
    if (aHasPerm === bHasPerm) return a.name.localeCompare(b.name);
    return aHasPerm ? -1 : 1; // Groups with permissions first
  });

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

              {/* Table */}
              <div className="border rounded-md flex-1 overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Group</TableHead>
                      <TableHead className="w-[180px]">
                        <div className="flex items-center gap-1">
                          Permission
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <HelpCircle className="h-3.5 w-3.5 text-muted-foreground cursor-help" />
                              </TooltipTrigger>
                              <TooltipContent>
                                <p className="max-w-xs"><strong>Read:</strong> Can activate<br /><strong>Write:</strong> Can edit and manage permissions</p>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        </div>
                      </TableHead>
                      <TableHead className="w-[140px]">
                        <div className="flex items-center gap-1">
                          Auto-enable
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <HelpCircle className="h-3.5 w-3.5 text-muted-foreground cursor-help" />
                              </TooltipTrigger>
                              <TooltipContent>
                                <p className="max-w-xs">When checked, this agent will be automatically activated for all members of the group</p>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        </div>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {displayGroups.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={3} className="text-center py-8 text-muted-foreground">
                          No groups match your search.
                        </TableCell>
                        </TableRow>
                      ) : (
                        displayGroups.map((group) => {
                        const permission = groupPermissions.get(group.id);
                        const role = permission?.role || 'none';
                        const memberCount = group.members?.length || 0;

                        return (
                          <TableRow key={group.id}>
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
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <div className="inline-flex">
                                      <Checkbox
                                        checked={defaultGroups.has(group.id)}
                                        onCheckedChange={(checked) => {
                                          const newDefaults = new Set(defaultGroups);
                                          if (checked) {
                                            newDefaults.add(group.id);
                                          } else {
                                            newDefaults.delete(group.id);
                                          }
                                          setDefaultGroups(newDefaults);
                                        }}
                                        disabled={role === 'none'}
                                      />
                                    </div>
                                  </TooltipTrigger>
                                  <TooltipContent>
                                    <p className="max-w-xs">
                                      {role === 'none' 
                                        ? 'Grant permissions first to enable auto-activation' 
                                        : 'This agent will be automatically activated for all current and new members of this group'}
                                    </p>
                                  </TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                            </TableCell>
                          </TableRow>
                        );
                      })
                    )}
                  </TableBody>
                </Table>
              </div>
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
