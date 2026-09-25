import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Users, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { GrantedGroupsTable, GroupGrantPicker } from '@/components/GroupGrantPicker';
import {
  getSecretPermissionsApiV1SecretsSecretIdPermissionsGetOptions,
  getSecretPermissionsApiV1SecretsSecretIdPermissionsGetQueryKey,
  updateSecretPermissionsApiV1SecretsSecretIdPermissionsPutMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { useGroupGrants } from '@/hooks/use-group-grants';
import { getErrorMessage } from '@/lib/utils';
import { toast } from 'sonner';

interface SecretPermissionsDialogProps {
  secretId: number;
  secretName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function SecretPermissionsDialog({
  secretId,
  secretName,
  open,
  onOpenChange,
}: SecretPermissionsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* A fixed height, not a max: the group list below changes length as you search
          and page, and a content-sized dialog re-centres and jumps on every keystroke. */}
      <DialogContent className="sm:max-w-3xl h-[85vh] flex flex-col">
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
        {/* Mounted only while open, so unsaved edits and the search reset on close. */}
        <SecretPermissionsBody secretId={secretId} onOpenChange={onOpenChange} />
      </DialogContent>
    </Dialog>
  );
}

function SecretPermissionsBody({
  secretId,
  onOpenChange,
}: Pick<SecretPermissionsDialogProps, 'secretId' | 'onOpenChange'>) {
  const queryClient = useQueryClient();

  // The whole grant set, unpaged: saving replaces it.
  const {
    data: currentPermissions,
    isLoading: isLoadingPermissions,
    error: permissionsError,
  } = useQuery({
    ...getSecretPermissionsApiV1SecretsSecretIdPermissionsGetOptions({
      path: { secret_id: secretId },
    }),
    retry: false,
  });

  const { grants, roleOf, setRole, hasChanges, toPermissions } = useGroupGrants(currentPermissions);

  const updateMutation = useMutation({
    ...updateSecretPermissionsApiV1SecretsSecretIdPermissionsPutMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: getSecretPermissionsApiV1SecretsSecretIdPermissionsGetQueryKey({
          path: { secret_id: secretId },
        }),
      });
      toast.success('Permissions updated successfully');
      onOpenChange(false);
    },
    onError: (err) => {
      toast.error('Failed to update permissions', { description: getErrorMessage(err) });
    },
  });

  const handleSave = () => {
    updateMutation.mutate({
      path: { secret_id: secretId },
      body: { group_permissions: toPermissions() },
    });
  };

  return (
    <>
      <div className="flex-1 overflow-y-auto min-h-0 space-y-6">
        {isLoadingPermissions ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : permissionsError ? (
          <div className="py-8 text-center">
            <p className="text-sm text-destructive mb-2">{getErrorMessage(permissionsError)}</p>
            <p className="text-xs text-muted-foreground">
              You don't have permission to manage access for this secret.
            </p>
          </div>
        ) : (
          <>
            <GrantedGroupsTable
              grants={grants}
              onRoleChange={setRole}
              roleHelp={
                <>
                  <strong>Read:</strong> Can use it in sub-agents
                  <br />
                  <strong>Write:</strong> Can edit and manage permissions
                </>
              }
              emptyMessage="Not shared with any group yet."
            />
            <GroupGrantPicker
              roleOf={roleOf}
              onGrant={setRole}
              emptyMessage="No groups available. Create groups to share this secret."
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
          disabled={!hasChanges || updateMutation.isPending || !!permissionsError}
        >
          {updateMutation.isPending && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          Save Changes
        </Button>
      </DialogFooter>
    </>
  );
}
