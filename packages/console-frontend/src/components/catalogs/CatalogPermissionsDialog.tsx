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
  getCatalogPermissionsOptions,
  setCatalogPermissionsMutation,
  getCatalogPermissionsQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import { useGroupGrants } from '@/hooks/use-group-grants';
import { toast } from 'sonner';
import { getErrorMessage } from '@/lib/utils';

interface CatalogPermissionsDialogProps {
  catalogId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function CatalogPermissionsDialog({ catalogId, open, onOpenChange }: CatalogPermissionsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* A fixed height, not a max: the group list below changes length as you search
          and page, and a content-sized dialog re-centres and jumps on every keystroke. */}
      <DialogContent className="sm:max-w-3xl h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Manage Permissions
          </DialogTitle>
          <DialogDescription>
            Control which groups can access this catalog.
          </DialogDescription>
        </DialogHeader>
        {/* Mounted only while open, so unsaved edits and the search reset on close. */}
        <CatalogPermissionsBody catalogId={catalogId} onOpenChange={onOpenChange} />
      </DialogContent>
    </Dialog>
  );
}

function CatalogPermissionsBody({
  catalogId,
  onOpenChange,
}: Pick<CatalogPermissionsDialogProps, 'catalogId' | 'onOpenChange'>) {
  const queryClient = useQueryClient();

  // The whole grant set, unpaged: saving replaces it.
  const {
    data: currentPermissions,
    isLoading: isLoadingPermissions,
    error: permissionsError,
  } = useQuery({
    ...getCatalogPermissionsOptions({ path: { catalog_id: catalogId } }),
    retry: false,
  });

  const { grants, roleOf, setRole, hasChanges, toPermissions } = useGroupGrants(currentPermissions);

  const saveMutation = useMutation({
    ...setCatalogPermissionsMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: getCatalogPermissionsQueryKey({ path: { catalog_id: catalogId } }) });
      toast.success('Permissions updated');
      onOpenChange(false);
    },
    onError: (err) => {
      toast.error('Failed to update permissions', { description: getErrorMessage(err) });
    },
  });

  const handleSave = () => {
    saveMutation.mutate({
      path: { catalog_id: catalogId },
      body: { permissions: toPermissions() },
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
              You don't have permission to manage access for this catalog.
            </p>
          </div>
        ) : (
          <>
            <GrantedGroupsTable
              grants={grants}
              onRoleChange={setRole}
              roleHelp={
                <>
                  <strong>Read:</strong> Can use the catalog
                  <br />
                  <strong>Write:</strong> Can also edit it and manage permissions
                </>
              }
              emptyMessage="Not shared with any group yet."
            />
            <GroupGrantPicker
              roleOf={roleOf}
              onGrant={setRole}
              emptyMessage="No groups available. You must be a member of a group to share access."
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
          disabled={!hasChanges || saveMutation.isPending || !!permissionsError}
        >
          {saveMutation.isPending ? 'Saving...' : 'Save'}
        </Button>
      </DialogFooter>
    </>
  );
}
