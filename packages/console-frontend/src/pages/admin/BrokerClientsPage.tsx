import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Pencil, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import {
  createBrokerClientApiV1AdminBrokerClientsPostMutation,
  deleteBrokerClientApiV1AdminBrokerClientsClientPkDeleteMutation,
  listBrokerClientsApiV1AdminBrokerClientsGetOptions,
  listBrokerClientsApiV1AdminBrokerClientsGetQueryKey,
  updateBrokerClientApiV1AdminBrokerClientsClientPkPatchMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { BrokerClient, BrokerClientUpdate } from '@/api/generated';
import { formatApiError } from '@/api/scheduler';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { TableRowsSkeleton } from '@/components/skeletons';
import { TableEmptyRow } from '@/components/EmptyState';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';

/** One entry per line; blank lines are dropped. */
function splitLines(value: string): string[] {
  return value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

export function BrokerClientsPage() {
  const queryClient = useQueryClient();
  const [formDialog, setFormDialog] = useState<{ open: boolean; brokerClient: BrokerClient | null }>({
    open: false,
    brokerClient: null,
  });
  const [deleteDialog, setDeleteDialog] = useState<{ open: boolean; brokerClient: BrokerClient | null }>({
    open: false,
    brokerClient: null,
  });

  // Form state
  const [clientId, setClientId] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [redirectUris, setRedirectUris] = useState('');
  const [audiences, setAudiences] = useState('');
  const [enabled, setEnabled] = useState(true);

  const editing = formDialog.brokerClient;

  const { data, isLoading } = useQuery(listBrokerClientsApiV1AdminBrokerClientsGetOptions());
  const refreshList = () =>
    queryClient.invalidateQueries({ queryKey: listBrokerClientsApiV1AdminBrokerClientsGetQueryKey() });

  const saved = (message: string) => () => {
    toast.success(message);
    closeForm();
    refreshList();
  };
  const createMutation = useMutation({
    ...createBrokerClientApiV1AdminBrokerClientsPostMutation(),
    onSuccess: saved('Broker client registered'),
    onError: (error) => {
      toast.error(`Failed to save broker client: ${formatApiError(error)}`);
    },
  });
  const updateMutation = useMutation({
    ...updateBrokerClientApiV1AdminBrokerClientsClientPkPatchMutation(),
    onSuccess: saved('Broker client updated'),
    onError: (error) => {
      toast.error(`Failed to save broker client: ${formatApiError(error)}`);
    },
  });
  const isSaving = createMutation.isPending || updateMutation.isPending;

  const toggleMutation = useMutation({
    ...updateBrokerClientApiV1AdminBrokerClientsClientPkPatchMutation(),
    onSuccess: refreshList,
    onError: (error) => {
      toast.error(`Failed to update broker client: ${formatApiError(error)}`);
    },
  });

  const deleteMutation = useMutation({
    ...deleteBrokerClientApiV1AdminBrokerClientsClientPkDeleteMutation(),
    onSuccess: () => {
      refreshList();
      toast.success('Broker client removed');
      setDeleteDialog({ open: false, brokerClient: null });
    },
    onError: (error) => {
      toast.error(`Failed to remove broker client: ${formatApiError(error)}`);
    },
  });

  const openForm = (brokerClient: BrokerClient | null) => {
    setClientId(brokerClient?.client_id ?? '');
    setName(brokerClient?.name ?? '');
    setDescription(brokerClient?.description ?? '');
    setRedirectUris(brokerClient?.redirect_uris.join('\n') ?? '');
    setAudiences(brokerClient?.audiences.join('\n') ?? '');
    setEnabled(brokerClient?.enabled ?? true);
    setFormDialog({ open: true, brokerClient });
  };

  const closeForm = () => setFormDialog({ open: false, brokerClient: null });

  const handleSave = () => {
    const body = {
      name: name.trim(),
      description: description.trim() || null,
      redirect_uris: splitLines(redirectUris),
      audiences: splitLines(audiences),
      enabled,
    } satisfies BrokerClientUpdate;
    if (editing) {
      updateMutation.mutate({ path: { client_pk: editing.id }, body });
    } else {
      createMutation.mutate({ body: { ...body, client_id: clientId.trim() } });
    }
  };

  const canSave =
    (editing || clientId.trim()) &&
    name.trim() &&
    splitLines(redirectUris).length > 0 &&
    splitLines(audiences).length > 0;

  const brokerClients = data?.clients ?? [];

  return (
    <div className="space-y-6 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Broker Clients</h1>
          <p className="text-muted-foreground">
            Applications that sign their users in through the console and get access tokens from it
          </p>
        </div>
        <Button onClick={() => openForm(null)}>
          <Plus className="h-4 w-4 mr-2" />
          Register Client
        </Button>
      </div>

      <div className="border rounded-lg">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Client ID</TableHead>
              <TableHead>Redirect URIs</TableHead>
              <TableHead>Audiences</TableHead>
              <TableHead>Enabled</TableHead>
              <TableHead className="w-24"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRowsSkeleton columns={6} />
            ) : brokerClients.length === 0 ? (
              <TableEmptyRow colSpan={6} title="No broker clients registered yet" />
            ) : (
              brokerClients.map((brokerClient) => (
                <TableRow key={brokerClient.id}>
                  <TableCell>
                    <div>
                      <span className="font-medium">{brokerClient.name}</span>
                      {brokerClient.description && (
                        <p className="text-xs text-muted-foreground mt-0.5">{brokerClient.description}</p>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <code className="text-xs bg-muted px-1.5 py-0.5 rounded">{brokerClient.client_id}</code>
                  </TableCell>
                  <TableCell>
                    <div className="space-y-0.5">
                      {brokerClient.redirect_uris.map((uri) => (
                        <div key={uri} className="text-xs font-mono break-all">
                          {uri}
                        </div>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {brokerClient.audiences.map((audience) => (
                        <Badge key={audience} variant="secondary">
                          {audience}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Switch
                      checked={brokerClient.enabled}
                      disabled={toggleMutation.isPending}
                      onCheckedChange={(checked) =>
                        toggleMutation.mutate({ path: { client_pk: brokerClient.id }, body: { enabled: checked } })
                      }
                    />
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => openForm(brokerClient)}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-destructive hover:text-destructive"
                        onClick={() => setDeleteDialog({ open: true, brokerClient })}
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

      {/* Register / Edit Dialog */}
      <Dialog open={formDialog.open} onOpenChange={(open) => !open && closeForm()}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? 'Edit Broker Client' : 'Register Broker Client'}</DialogTitle>
            <DialogDescription>
              The client ID is the Keycloak client that calls the broker with its own
              client-credentials token.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="broker-client-id">Client ID</Label>
              <Input
                id="broker-client-id"
                placeholder="e.g. cockpit-embed"
                value={clientId}
                disabled={!!editing}
                onChange={(e) => setClientId(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="broker-client-name">Name</Label>
              <Input
                id="broker-client-name"
                placeholder="e.g. Cockpit"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="broker-client-description">Description (optional)</Label>
              <Input
                id="broker-client-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="broker-client-redirect-uris">Redirect URIs (one per line)</Label>
              <Textarea
                id="broker-client-redirect-uris"
                className="font-mono text-xs"
                placeholder={
                  'https://riad.d.alloy.ch/nannos-auth-callback.html\nhttps://pr-*-riad.d.alloy.ch/nannos-auth-callback.html'
                }
                value={redirectUris}
                onChange={(e) => setRedirectUris(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Exact match. Only the first host label may contain <code>*</code>, for preview
                environments.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="broker-client-audiences">Audiences (one per line)</Label>
              <Textarea
                id="broker-client-audiences"
                className="font-mono text-xs"
                placeholder="e.g. cockpit-embed"
                value={audiences}
                onChange={(e) => setAudiences(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Switch id="broker-client-enabled" checked={enabled} onCheckedChange={setEnabled} />
                <Label htmlFor="broker-client-enabled">Enabled</Label>
              </div>
              <p className="text-xs text-muted-foreground">
                Turning a client off stops its sign-ins and tokens, but keeps its users signed in
                for when it is on again. A change can take up to a minute to reach every server.
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeForm}>
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={!canSave || isSaving}>
              {isSaving ? 'Saving...' : editing ? 'Save' : 'Register'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirm Dialog */}
      {deleteDialog.brokerClient && (
        <ConfirmDialog
          open={deleteDialog.open}
          onOpenChange={(open) => !open && setDeleteDialog({ open: false, brokerClient: null })}
          title="Remove Broker Client"
          description={`Remove "${deleteDialog.brokerClient.name}"? Every user who signed in through it is signed out of it, and must sign in again, also if you register it again. Their console sign-in is not affected. To stop the client only for a time, turn it off instead: that keeps its users signed in.`}
          confirmLabel="Remove"
          variant="destructive"
          onConfirm={() => deleteMutation.mutate({ path: { client_pk: deleteDialog.brokerClient!.id } })}
          isLoading={deleteMutation.isPending}
        />
      )}
    </div>
  );
}
