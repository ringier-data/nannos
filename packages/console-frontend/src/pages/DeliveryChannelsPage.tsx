import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Pencil, Trash2, Webhook, Plus, X, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  getDeliveryChannels,
  updateDeliveryChannel,
  deleteDeliveryChannel,
  type DeliveryChannel,
  type DeliveryChannelUpdate,
} from '@/api/scheduler';
import { listMyGroupsApiV1GroupsGetOptions } from '@/api/generated/@tanstack/react-query.gen';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function truncateUrl(url: string, max = 50): string {
  if (url.length <= max) return url;
  return url.slice(0, max) + '…';
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

// ---------------------------------------------------------------------------
// Edit dialog
// ---------------------------------------------------------------------------

interface EditDialogProps {
  channel: DeliveryChannel;
  groupMap: Map<number, string>;
  onClose: () => void;
}

function EditDialog({ channel, groupMap, onClose }: EditDialogProps) {
  const qc = useQueryClient();
  const [name, setName] = useState(channel.name);
  const [description, setDescription] = useState(channel.description ?? '');
  const [webhookUrl, setWebhookUrl] = useState(channel.webhook_url);
  const [secret, setSecret] = useState('');
  const [groupIdsText, setGroupIdsText] = useState(channel.group_ids.join(', '));
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (patch: DeliveryChannelUpdate) => updateDeliveryChannel(channel.id, patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['delivery-channels'] });
      onClose();
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  function handleSave() {
    setError(null);

    // Parse group IDs
    const rawIds = groupIdsText
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    const group_ids = rawIds.map(Number);
    if (group_ids.some(isNaN)) {
      setError('Group IDs must be comma-separated integers.');
      return;
    }

    const patch: DeliveryChannelUpdate = {
      name: name || undefined,
      description: description || null,
      webhook_url: webhookUrl || undefined,
      group_ids: group_ids.length > 0 ? group_ids : undefined,
    };
    if (secret) patch.secret = secret;

    mutation.mutate(patch);
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit delivery channel</DialogTitle>
          <DialogDescription>
            Update the channel configuration. Leave <strong>New secret</strong> blank to keep the
            current secret.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 py-2">
          <div className="grid gap-1.5">
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </div>

          <div className="grid gap-1.5">
            <Label>
              Description{' '}
              <span className="text-muted-foreground text-xs">(optional — used by LLM to select channel)</span>
            </Label>
            <Textarea
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="e.g. Sends push notifications to the mobile app for critical alerts"
            />
          </div>

          <div className="grid gap-1.5">
            <Label>Webhook URL</Label>
            <Input
              type="url"
              value={webhookUrl}
              onChange={(e) => setWebhookUrl(e.target.value)}
            />
          </div>

          <div className="grid gap-1.5">
            <Label>New secret (X-A2A-Notification-Token)</Label>
            <Input
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder="Leave blank to keep current secret"
            />
          </div>

          <div className="grid gap-1.5">
            <Label>
              Group IDs{' '}
              <span className="text-muted-foreground text-xs">(comma-separated)</span>
            </Label>
            <Input
              value={groupIdsText}
              onChange={(e) => setGroupIdsText(e.target.value)}
              placeholder="1, 2, 3"
            />
            {groupMap.size > 0 && channel.group_ids.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-0.5">
                {channel.group_ids.map((gid) => (
                  <Badge key={gid} variant="outline" className="text-xs">
                    {groupMap.get(gid) ?? `Group ${gid}`}
                  </Badge>
                ))}
              </div>
            )}
          </div>
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={mutation.isPending}>
            {mutation.isPending ? 'Saving…' : (
              <>
                <Check className="mr-1.5 h-4 w-4" />
                Save changes
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function DeliveryChannelsPage() {
  const qc = useQueryClient();
  const [editChannel, setEditChannel] = useState<DeliveryChannel | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeliveryChannel | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const { data: channels = [], isLoading, error } = useQuery<DeliveryChannel[]>({
    queryKey: ['delivery-channels'],
    queryFn: getDeliveryChannels,
    staleTime: 30_000,
  });

  // Fetch the user's groups to resolve group IDs → names
  const { data: myGroupsData } = useQuery(listMyGroupsApiV1GroupsGetOptions());
  const groupMap = new Map<number, string>(
    (Array.isArray(myGroupsData) ? myGroupsData : []).map((g: { id: number; name: string }) => [g.id, g.name]),
  );

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteDeliveryChannel(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['delivery-channels'] });
      setDeleteTarget(null);
      setDeleteError(null);
    },
    onError: (e: unknown) => setDeleteError(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="container mx-auto max-w-5xl py-6 px-4 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <Webhook className="h-6 w-6" />
            Delivery Channels
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            A2A clients register push-notification webhooks here. The scheduler uses these channels
            to deliver job results and watch alerts.
          </p>
        </div>
      </div>

      {/* Registration hint */}
      <div className="rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground">
        <p className="flex items-center gap-1.5">
          <Plus className="h-4 w-4 shrink-0" />
          <span>
            Channels are registered programmatically via{' '}
            <code className="rounded bg-muted px-1 font-mono text-xs">
              POST /api/v1/delivery-channels
            </code>{' '}
            using a Keycloak client-credentials token.
          </span>
        </p>
      </div>

      {/* Table */}
      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : error ? (
        <p className="text-sm text-destructive">Failed to load delivery channels.</p>
      ) : channels.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-16 text-muted-foreground">
          <Webhook className="h-10 w-10 opacity-30" />
          <p className="text-sm">No delivery channels registered yet.</p>
        </div>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Webhook URL</TableHead>
                <TableHead>Groups</TableHead>
                <TableHead>Client</TableHead>
                <TableHead>Registered</TableHead>
                <TableHead className="w-[100px]" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {channels.map((ch) => (
                <TableRow key={ch.id}>
                  <TableCell>
                    <div className="font-medium">{ch.name}</div>
                    {ch.description && (
                      <div className="text-xs text-muted-foreground mt-0.5 max-w-[220px] truncate">
                        {ch.description}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <code className="text-xs font-mono text-muted-foreground">
                      {truncateUrl(ch.webhook_url)}
                    </code>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {ch.group_ids.length === 0 ? (
                        <span className="text-xs text-muted-foreground">—</span>
                      ) : (
                        ch.group_ids.map((gid) => (
                          <Badge key={gid} variant="outline" className="text-xs">
                            {groupMap.get(gid) ?? `#${gid}`}
                          </Badge>
                        ))
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <code className="text-xs font-mono text-muted-foreground">{ch.client_id}</code>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatDate(ch.created_at)}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1 justify-end">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        title="Edit channel"
                        onClick={() => setEditChannel(ch)}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-destructive hover:text-destructive"
                        title="Delete channel"
                        onClick={() => { setDeleteTarget(ch); setDeleteError(null); }}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* Edit dialog */}
      {editChannel && (
        <EditDialog
          channel={editChannel}
          groupMap={groupMap}
          onClose={() => setEditChannel(null)}
        />
      )}

      {/* Delete confirmation */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(o) => { if (!o) { setDeleteTarget(null); setDeleteError(null); } }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete delivery channel?</AlertDialogTitle>
            <AlertDialogDescription>
              <strong>{deleteTarget?.name}</strong> will be permanently deleted. Any scheduler
              jobs still referencing this channel will fail to deliver notifications.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteError && <p className="text-sm text-destructive px-1">{deleteError}</p>}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteMutation.isPending}
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
            >
              {deleteMutation.isPending ? 'Deleting…' : (
                <>
                  <Trash2 className="mr-1.5 h-4 w-4" />
                  Delete
                </>
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
