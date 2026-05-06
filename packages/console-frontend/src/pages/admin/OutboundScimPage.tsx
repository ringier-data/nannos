import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, TestTube, Pencil, Globe } from 'lucide-react';
import { toast } from 'sonner';
import { client } from '@/api/generated/client.gen';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
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
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';

interface OutboundScimEndpoint {
  id: string;
  name: string;
  endpoint_url: string;
  token_hint: string;
  enabled: boolean;
  push_users: boolean;
  push_groups: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
}

interface OutboundScimEndpointCreated {
  id: string;
  name: string;
  endpoint_url: string;
  bearer_token: string;
  enabled: boolean;
  push_users: boolean;
  push_groups: boolean;
  created_at: string;
}

interface OutboundScimListResponse {
  data: OutboundScimEndpoint[];
  meta: { page: number; limit: number; total: number };
}

interface TestResult {
  success: boolean;
  status_code: number | null;
  message: string;
}

const API_BASE = '/api/v1/admin/outbound-scim-endpoints';

export function OutboundScimPage() {
  const queryClient = useQueryClient();
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialog, setEditDialog] = useState<{ open: boolean; endpoint: OutboundScimEndpoint | null }>({
    open: false,
    endpoint: null,
  });
  const [deleteDialog, setDeleteDialog] = useState<{ open: boolean; endpoint: OutboundScimEndpoint | null }>({
    open: false,
    endpoint: null,
  });

  // Create form state
  const [name, setName] = useState('');
  const [endpointUrl, setEndpointUrl] = useState('');
  const [bearerToken, setBearerToken] = useState('');
  const [pushUsers, setPushUsers] = useState(true);
  const [pushGroups, setPushGroups] = useState(true);

  // Edit form state
  const [editName, setEditName] = useState('');
  const [editEndpointUrl, setEditEndpointUrl] = useState('');
  const [editBearerToken, setEditBearerToken] = useState('');
  const [editPushUsers, setEditPushUsers] = useState(true);
  const [editPushGroups, setEditPushGroups] = useState(true);
  const [editEnabled, setEditEnabled] = useState(true);

  const { data, isLoading } = useQuery<OutboundScimListResponse>({
    queryKey: ['outboundScimEndpoints'],
    queryFn: async () => {
      const res = await client.get<OutboundScimListResponse>({ url: API_BASE });
      return res.data as OutboundScimListResponse;
    },
  });

  const createMutation = useMutation({
    mutationFn: async (body: { name: string; endpoint_url: string; bearer_token: string; push_users: boolean; push_groups: boolean }) => {
      const res = await client.post<OutboundScimEndpointCreated>({ url: API_BASE, body });
      return res.data as OutboundScimEndpointCreated;
    },
    onSuccess: () => {
      setCreateDialogOpen(false);
      resetCreateForm();
      queryClient.invalidateQueries({ queryKey: ['outboundScimEndpoints'] });
      toast.success('Outbound SCIM endpoint created');
    },
    onError: () => {
      toast.error('Failed to create endpoint');
    },
  });

  const updateMutation = useMutation({
    mutationFn: async ({ id, body }: { id: string; body: Record<string, unknown> }) => {
      const res = await client.patch({ url: `${API_BASE}/${id}`, body });
      return res.data;
    },
    onSuccess: () => {
      setEditDialog({ open: false, endpoint: null });
      queryClient.invalidateQueries({ queryKey: ['outboundScimEndpoints'] });
      toast.success('Endpoint updated');
    },
    onError: () => {
      toast.error('Failed to update endpoint');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (id: string) => {
      await client.delete({ url: `${API_BASE}/${id}` });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['outboundScimEndpoints'] });
      toast.success('Endpoint deleted');
      setDeleteDialog({ open: false, endpoint: null });
    },
    onError: () => {
      toast.error('Failed to delete endpoint');
    },
  });

  const testMutation = useMutation({
    mutationFn: async (id: string) => {
      const res = await client.post<TestResult>({ url: `${API_BASE}/${id}/test` });
      return res.data as TestResult;
    },
    onSuccess: (result) => {
      if (result.success) {
        toast.success(`Connection successful (${result.status_code})`);
      } else {
        toast.error(`Connection failed: ${result.message}`);
      }
    },
    onError: () => {
      toast.error('Test request failed');
    },
  });

  const resetCreateForm = () => {
    setName('');
    setEndpointUrl('');
    setBearerToken('');
    setPushUsers(true);
    setPushGroups(true);
  };

  const handleCreate = () => {
    createMutation.mutate({ name, endpoint_url: endpointUrl, bearer_token: bearerToken, push_users: pushUsers, push_groups: pushGroups });
  };

  const openEditDialog = (endpoint: OutboundScimEndpoint) => {
    setEditName(endpoint.name);
    setEditEndpointUrl(endpoint.endpoint_url);
    setEditBearerToken('');
    setEditPushUsers(endpoint.push_users);
    setEditPushGroups(endpoint.push_groups);
    setEditEnabled(endpoint.enabled);
    setEditDialog({ open: true, endpoint });
  };

  const handleUpdate = () => {
    if (!editDialog.endpoint) return;
    const body: Record<string, unknown> = {
      name: editName,
      endpoint_url: editEndpointUrl,
      push_users: editPushUsers,
      push_groups: editPushGroups,
      enabled: editEnabled,
    };
    if (editBearerToken) body.bearer_token = editBearerToken;
    updateMutation.mutate({ id: editDialog.endpoint.id, body });
  };

  const endpoints = data?.data ?? [];

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return '—';
    return new Date(dateStr).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  };

  return (
    <div className="space-y-6 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Outbound SCIM Endpoints</h1>
          <p className="text-muted-foreground">
            Push user and group changes to external SCIM 2.0 servers
          </p>
        </div>
        <Button onClick={() => setCreateDialogOpen(true)}>
          <Plus className="h-4 w-4 mr-2" />
          Add Endpoint
        </Button>
      </div>

      {/* Endpoints Table */}
      <div className="border rounded-lg">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>URL</TableHead>
              <TableHead>Pushes</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Created</TableHead>
              <TableHead className="w-28"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center py-8">
                  Loading...
                </TableCell>
              </TableRow>
            ) : endpoints.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center py-8 text-muted-foreground">
                  <Globe className="h-8 w-8 mx-auto mb-2 opacity-50" />
                  No outbound SCIM endpoints configured
                </TableCell>
              </TableRow>
            ) : (
              endpoints.map((ep) => (
                <TableRow key={ep.id}>
                  <TableCell className="font-medium">{ep.name}</TableCell>
                  <TableCell>
                    <code className="text-xs bg-muted px-1.5 py-0.5 rounded truncate max-w-[200px] block">
                      {ep.endpoint_url}
                    </code>
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-1">
                      {ep.push_users && <Badge variant="secondary">Users</Badge>}
                      {ep.push_groups && <Badge variant="secondary">Groups</Badge>}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={ep.enabled ? 'default' : 'secondary'}>
                      {ep.enabled ? 'Active' : 'Disabled'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm">{formatDate(ep.created_at)}</TableCell>
                  <TableCell>
                    <div className="flex gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => testMutation.mutate(ep.id)}
                        disabled={testMutation.isPending}
                        title="Test connection"
                      >
                        <TestTube className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => openEditDialog(ep)}
                        title="Edit"
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-destructive hover:text-destructive"
                        onClick={() => setDeleteDialog({ open: true, endpoint: ep })}
                        title="Delete"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      {/* Create Dialog */}
      <Dialog
        open={createDialogOpen}
        onOpenChange={(open) => {
          setCreateDialogOpen(open);
          if (!open) resetCreateForm();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Outbound SCIM Endpoint</DialogTitle>
            <DialogDescription>
              Configure an external SCIM 2.0 server to receive user and group provisioning events.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="ep-name">Name</Label>
              <Input
                id="ep-name"
                placeholder="e.g. Salesforce SCIM"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="ep-url">SCIM Base URL</Label>
              <Input
                id="ep-url"
                placeholder="https://api.example.com/scim/v2"
                value={endpointUrl}
                onChange={(e) => setEndpointUrl(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="ep-token">Bearer Token</Label>
              <Input
                id="ep-token"
                type="password"
                placeholder="Bearer token for authentication"
                value={bearerToken}
                onChange={(e) => setBearerToken(e.target.value)}
              />
            </div>
            <div className="flex items-center gap-6">
              <div className="flex items-center gap-2">
                <Switch id="ep-push-users" checked={pushUsers} onCheckedChange={setPushUsers} />
                <Label htmlFor="ep-push-users">Push Users</Label>
              </div>
              <div className="flex items-center gap-2">
                <Switch id="ep-push-groups" checked={pushGroups} onCheckedChange={setPushGroups} />
                <Label htmlFor="ep-push-groups">Push Groups</Label>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleCreate}
              disabled={!name.trim() || !endpointUrl.trim() || !bearerToken.trim() || createMutation.isPending}
            >
              {createMutation.isPending ? 'Creating...' : 'Add Endpoint'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Dialog */}
      <Dialog
        open={editDialog.open}
        onOpenChange={(open) => {
          if (!open) setEditDialog({ open: false, endpoint: null });
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Endpoint</DialogTitle>
            <DialogDescription>
              Update endpoint configuration. Leave bearer token empty to keep the existing one.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="edit-name">Name</Label>
              <Input
                id="edit-name"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-url">SCIM Base URL</Label>
              <Input
                id="edit-url"
                value={editEndpointUrl}
                onChange={(e) => setEditEndpointUrl(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-token">Bearer Token (leave empty to keep current)</Label>
              <Input
                id="edit-token"
                type="password"
                placeholder="••••••••"
                value={editBearerToken}
                onChange={(e) => setEditBearerToken(e.target.value)}
              />
            </div>
            <div className="flex items-center gap-6">
              <div className="flex items-center gap-2">
                <Switch id="edit-push-users" checked={editPushUsers} onCheckedChange={setEditPushUsers} />
                <Label htmlFor="edit-push-users">Push Users</Label>
              </div>
              <div className="flex items-center gap-2">
                <Switch id="edit-push-groups" checked={editPushGroups} onCheckedChange={setEditPushGroups} />
                <Label htmlFor="edit-push-groups">Push Groups</Label>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Switch id="edit-enabled" checked={editEnabled} onCheckedChange={setEditEnabled} />
              <Label htmlFor="edit-enabled">Enabled</Label>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialog({ open: false, endpoint: null })}>
              Cancel
            </Button>
            <Button
              onClick={handleUpdate}
              disabled={!editName.trim() || !editEndpointUrl.trim() || updateMutation.isPending}
            >
              {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirm Dialog */}
      {deleteDialog.endpoint && (
        <ConfirmDialog
          open={deleteDialog.open}
          onOpenChange={(open) => !open && setDeleteDialog({ open: false, endpoint: null })}
          title="Delete Endpoint"
          description={`Are you sure you want to delete "${deleteDialog.endpoint.name}"? Provisioning to this endpoint will stop immediately.`}
          confirmLabel="Delete"
          variant="destructive"
          onConfirm={() => deleteMutation.mutate(deleteDialog.endpoint!.id)}
          isLoading={deleteMutation.isPending}
        />
      )}
    </div>
  );
}
