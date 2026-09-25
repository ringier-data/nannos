import { useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Users, Loader2, HelpCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
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
import { GrantedGroupsTable, GroupGrantPicker } from '@/components/GroupGrantPicker';
import {
  getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetOptions,
  getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetQueryKey,
  updateSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsPutMutation,
  getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGetOptions,
  addGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdPostMutation,
  removeGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdDeleteMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { useGroupDefaults } from '@/hooks/use-group-defaults';
import { useGroupGrants, type GrantRole, type GrantTarget } from '@/hooks/use-group-grants';
import { getErrorMessage } from '@/lib/utils';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';

interface SubAgentPermissionsDialogProps {
  subAgentId: number;
  subAgentName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function SubAgentPermissionsDialog({
  subAgentId,
  subAgentName,
  open,
  onOpenChange,
}: SubAgentPermissionsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* A fixed height, not a max: the group list below changes length as you search
          and page, and a content-sized dialog re-centres and jumps on every keystroke. */}
      <DialogContent className="sm:max-w-3xl h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Manage Group Permissions
          </DialogTitle>
          <DialogDescription>
            Assign read and write permissions to groups for "{subAgentName}".
          </DialogDescription>
        </DialogHeader>
        {/* Mounted only while open, so unsaved edits and the search reset on close. */}
        <SubAgentPermissionsBody subAgentId={subAgentId} onOpenChange={onOpenChange} />
      </DialogContent>
    </Dialog>
  );
}

function SubAgentPermissionsBody({
  subAgentId,
  onOpenChange,
}: Pick<SubAgentPermissionsDialogProps, 'subAgentId' | 'onOpenChange'>) {
  const queryClient = useQueryClient();
  const { isAdmin, adminMode } = useAuth();

  // Only use admin group API if admin mode is enabled
  const canUseAdminGroups = isAdmin && adminMode;

  // The whole grant set, unpaged: saving replaces it.
  const {
    data: currentPermissions,
    isLoading: isLoadingPermissions,
    error: permissionsError,
  } = useQuery({
    ...getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetOptions({
      path: { sub_agent_id: subAgentId },
    }),
    retry: false,
  });

  const { grants, roleOf, setRole, hasChanges: grantsChanged, toPermissions } =
    useGroupGrants(currentPermissions);

  const grantedGroupIds = useMemo(
    () => currentPermissions?.map((perm) => perm.user_group_id) ?? [],
    [currentPermissions],
  );

  // Each group's accessible-agents list is read whole: the question is whether ONE
  // agent id is in it, and there is no id filter, so a page of it could miss it.
  const {
    defaults,
    added,
    removed,
    hasChanges: defaultsChanged,
    setDefault,
    isLoading: isLoadingDefaults,
  } = useGroupDefaults({
    queryKey: ['sub-agent-group-defaults', subAgentId],
    groupIds: grantedGroupIds,
    isDefaultFor: async (groupId) => {
      const agents = await queryClient.fetchQuery(
        getGroupAccessibleAgentsApiV1GroupsGroupIdAccessibleAgentsGetOptions({
          path: { group_id: groupId },
        }),
      );
      // The list holds every agent the group may use; `is_default` is the flag.
      return agents.some((agent) => agent.id === subAgentId && agent.is_default);
    },
    granted: (groupId) => roleOf(groupId) !== undefined,
  });

  const changeRole = (group: GrantTarget, role: GrantRole | null) => {
    // Auto-enable default when granting a group access; a revoked grant takes its
    // default with it.
    if (role === null) setDefault(group.id, false);
    else if (roleOf(group.id) === undefined) setDefault(group.id, true);
    setRole(group, role);
  };

  const updateMutation = useMutation({
    ...updateSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsPutMutation(),
  });

  const addDefaultMutation = useMutation({
    ...addGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdPostMutation(),
  });

  const removeDefaultMutation = useMutation({
    ...removeGroupDefaultAgentApiV1GroupsGroupIdDefaultAgentsSubAgentIdDeleteMutation(),
  });

  const handleSave = async () => {
    try {
      // Permissions first: a default is only accepted for a group holding the grant.
      await updateMutation.mutateAsync({
        path: { sub_agent_id: subAgentId },
        body: { group_permissions: toPermissions() },
      });

      await Promise.all([
        ...added.map((groupId) =>
          addDefaultMutation.mutateAsync({
            path: { group_id: groupId, sub_agent_id: subAgentId },
          }),
        ),
        ...removed.map((groupId) =>
          removeDefaultMutation.mutateAsync({
            path: { group_id: groupId, sub_agent_id: subAgentId },
          }),
        ),
      ]);

      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: getSubAgentPermissionsApiV1SubAgentsSubAgentIdPermissionsGetQueryKey({
            path: { sub_agent_id: subAgentId },
          }),
        }),
        queryClient.invalidateQueries({ queryKey: ['sub-agent-group-defaults', subAgentId] }),
      ]);
      toast.success('Permissions and defaults updated successfully');
      onOpenChange(false);
    } catch (err) {
      // Also a failed default change, which used to vanish without a word.
      toast.error('Failed to update permissions', { description: getErrorMessage(err) });
    }
  };

  const isSaving =
    updateMutation.isPending || addDefaultMutation.isPending || removeDefaultMutation.isPending;

  return (
    <>
      <div className="flex-1 overflow-y-auto min-h-0 space-y-6">
        {isLoadingPermissions ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading permissions...
          </div>
        ) : permissionsError ? (
          <div className="py-8 text-center">
            <p className="text-sm text-destructive mb-2">{getErrorMessage(permissionsError)}</p>
            <p className="text-xs text-muted-foreground">
              You don't have permission to manage access for this sub-agent.
            </p>
          </div>
        ) : (
          <>
            <GrantedGroupsTable
              grants={grants}
              onRoleChange={changeRole}
              roleHelp={
                <>
                  <strong>Read:</strong> Can activate
                  <br />
                  <strong>Write:</strong> Can edit and manage permissions
                </>
              }
              extraColumn={{
                className: 'w-[140px]',
                header: (
                  <div className="flex items-center gap-1">
                    Auto-enable
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <HelpCircle className="h-3.5 w-3.5 text-muted-foreground cursor-help" />
                        </TooltipTrigger>
                        <TooltipContent>
                          <p className="max-w-xs">
                            When checked, this agent will be automatically activated for all
                            members of the group
                          </p>
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  </div>
                ),
                cell: (grant) => (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <div className="inline-flex">
                          <Checkbox
                            checked={defaults.has(grant.groupId)}
                            disabled={isLoadingDefaults}
                            onCheckedChange={(checked) => setDefault(grant.groupId, checked === true)}
                          />
                        </div>
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">
                          This agent will be automatically activated for all current and new
                          members of this group
                        </p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                ),
              }}
              emptyMessage="Not shared with any group yet."
            />
            <GroupGrantPicker
              // An administrator in admin mode shares to any group in the org.
              scope={canUseAdminGroups ? 'all' : 'mine'}
              roleOf={roleOf}
              onGrant={changeRole}
              emptyMessage={
                isAdmin
                  ? 'No groups available.'
                  : 'No groups available. You must be a member of a group to share access.'
              }
            />
          </>
        )}
      </div>

      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
        <Button
          onClick={handleSave}
          disabled={(!grantsChanged && !defaultsChanged) || isSaving || !!permissionsError}
        >
          {isSaving ? 'Saving...' : 'Save Changes'}
        </Button>
      </DialogFooter>
    </>
  );
}
