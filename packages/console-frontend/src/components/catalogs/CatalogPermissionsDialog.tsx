import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Users, Search } from 'lucide-react';
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
  getCatalogPermissionsOptions,
  setCatalogPermissionsMutation,
  getCatalogPermissionsQueryKey,
  listMyGroupsApiV1GroupsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { UserGroupWithMembers } from '@/api/generated/types.gen';
import { toast } from 'sonner';
import { getErrorMessage } from '@/lib/utils';

interface CatalogPermissionsDialogProps {
  catalogId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function CatalogPermissionsDialog({ catalogId, open, onOpenChange }: CatalogPermissionsDialogProps) {
  const queryClient = useQueryClient();
  const [permMap, setPermMap] = useState<Map<number, 'none' | 'read' | 'write'>>(new Map());
  const [hasChanges, setHasChanges] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  const { data: currentPermissions } = useQuery({
    ...getCatalogPermissionsOptions({ path: { catalog_id: catalogId } }),
    enabled: open,
  });

  const { data: groupsData } = useQuery({
    ...listMyGroupsApiV1GroupsGetOptions(),
    enabled: open,
  });

  const availableGroups: UserGroupWithMembers[] = groupsData ?? [];

  const filteredGroups = availableGroups.filter((g) => {
    if (!searchQuery) return true;
    return g.name.toLowerCase().includes(searchQuery.toLowerCase());
  });

  // Sync state on load
  useEffect(() => {
    if (currentPermissions) {
      const m = new Map<number, 'none' | 'read' | 'write'>();
      for (const perm of currentPermissions) {
        const hasWrite = perm.permissions?.includes('write');
        const hasRead = perm.permissions?.includes('read');
        m.set(perm.user_group_id, hasWrite ? 'write' : hasRead ? 'read' : 'none');
      }
      setPermMap(m);
      setHasChanges(false);
    }
  }, [currentPermissions]);

  useEffect(() => {
    if (!open) {
      setHasChanges(false);
      setSearchQuery('');
    }
  }, [open]);

  const handleRoleChange = (groupId: number, role: 'none' | 'read' | 'write') => {
    setPermMap((prev) => {
      const m = new Map(prev);
      if (role === 'none') {
        m.delete(groupId);
      } else {
        m.set(groupId, role);
      }
      return m;
    });
    setHasChanges(true);
  };

  const saveMutation = useMutation({
    ...setCatalogPermissionsMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: getCatalogPermissionsQueryKey({ path: { catalog_id: catalogId } }) });
      toast.success('Permissions updated');
      setHasChanges(false);
    },
    onError: (err) => {
      toast.error('Failed to update permissions', { description: getErrorMessage(err) });
    },
  });

  const handleSave = () => {
    const permissions = Array.from(permMap.entries())
      .filter(([, role]) => role !== 'none')
      .map(([groupId, role]) => ({
        user_group_id: groupId,
        permissions: role === 'write' ? ['read', 'write'] : ['read'],
      }));

    saveMutation.mutate({
      path: { catalog_id: catalogId },
      body: { permissions },
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Manage Permissions
          </DialogTitle>
          <DialogDescription>
            Control which groups can access this catalog.
          </DialogDescription>
        </DialogHeader>

        <div className="relative mb-2">
          <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search groups..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-8"
          />
        </div>

        <div className="max-h-[400px] overflow-auto border rounded-md">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Group</TableHead>
                <TableHead className="w-[140px]">Access</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredGroups.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={2} className="text-center text-muted-foreground">
                    No groups available
                  </TableCell>
                </TableRow>
              ) : (
                filteredGroups.map((group) => (
                  <TableRow key={group.id}>
                    <TableCell>
                      <div>
                        <span className="font-medium text-sm">{group.name}</span>
                        {group.description && (
                          <p className="text-xs text-muted-foreground">{group.description}</p>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Select
                        value={permMap.get(group.id) ?? 'none'}
                        onValueChange={(v) => handleRoleChange(group.id, v as 'none' | 'read' | 'write')}
                      >
                        <SelectTrigger className="h-8 text-xs">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="none">No Access</SelectItem>
                          <SelectItem value="read">Read</SelectItem>
                          <SelectItem value="write">Read & Write</SelectItem>
                        </SelectContent>
                      </Select>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={!hasChanges || saveMutation.isPending}>
            {saveMutation.isPending ? 'Saving...' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
