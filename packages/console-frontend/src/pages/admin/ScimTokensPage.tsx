import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, Copy, Check, Key } from 'lucide-react';
import { toast } from 'sonner';
import { client } from '@/api/generated/client.gen';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
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

interface ScimToken {
  id: number;
  name: string;
  description: string | null;
  token_hint: string;
  created_by: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

interface ScimTokenCreated {
  id: number;
  name: string;
  description: string | null;
  token: string;
  expires_at: string | null;
  created_at: string;
}

interface ScimTokenListResponse {
  data: ScimToken[];
  meta: { page: number; limit: number; total: number };
}

export function ScimTokensPage() {
  const queryClient = useQueryClient();
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [createdToken, setCreatedToken] = useState<ScimTokenCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokeDialog, setRevokeDialog] = useState<{ open: boolean; token: ScimToken | null }>({
    open: false,
    token: null,
  });

  // Form state
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [expiresAt, setExpiresAt] = useState('');

  const { data, isLoading } = useQuery<ScimTokenListResponse>({
    queryKey: ['scimTokens'],
    queryFn: async () => {
      const res = await client.get<ScimTokenListResponse>({
        url: '/api/v1/admin/scim-tokens',
      });
      return res.data as ScimTokenListResponse;
    },
  });

  const createMutation = useMutation({
    mutationFn: async (body: { name: string; description?: string; expires_at?: string }) => {
      const res = await client.post<ScimTokenCreated>({
        url: '/api/v1/admin/scim-tokens',
        body,
      });
      return res.data as ScimTokenCreated;
    },
    onSuccess: (token) => {
      setCreatedToken(token);
      setCreateDialogOpen(false);
      resetForm();
      queryClient.invalidateQueries({ queryKey: ['scimTokens'] });
      toast.success('SCIM token created');
    },
    onError: () => {
      toast.error('Failed to create SCIM token');
    },
  });

  const revokeMutation = useMutation({
    mutationFn: async (tokenId: number) => {
      await client.delete({ url: `/api/v1/admin/scim-tokens/${tokenId}` });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scimTokens'] });
      toast.success('SCIM token revoked');
      setRevokeDialog({ open: false, token: null });
    },
    onError: () => {
      toast.error('Failed to revoke SCIM token');
    },
  });

  const resetForm = () => {
    setName('');
    setDescription('');
    setExpiresAt('');
  };

  const handleCreate = () => {
    const body: { name: string; description?: string; expires_at?: string } = { name };
    if (description) body.description = description;
    if (expiresAt) body.expires_at = new Date(expiresAt).toISOString();
    createMutation.mutate(body);
  };

  const handleCopy = async (token: string) => {
    await navigator.clipboard.writeText(token);
    setCopied(true);
    toast.success('Token copied to clipboard');
    setTimeout(() => setCopied(false), 2000);
  };

  const tokens = data?.data ?? [];

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return '—';
    return new Date(dateStr).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const getTokenStatus = (token: ScimToken) => {
    if (token.revoked_at) return 'revoked';
    if (token.expires_at && new Date(token.expires_at) < new Date()) return 'expired';
    return 'active';
  };

  return (
    <div className="space-y-6 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">SCIM Tokens</h1>
          <p className="text-muted-foreground">
            Manage bearer tokens for SCIM 2.0 provisioning
          </p>
        </div>
        <Button onClick={() => setCreateDialogOpen(true)}>
          <Plus className="h-4 w-4 mr-2" />
          Create Token
        </Button>
      </div>

      {/* Token Table */}
      <div className="border rounded-lg">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Token</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Last Used</TableHead>
              <TableHead>Expires</TableHead>
              <TableHead className="w-12"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8">
                  Loading...
                </TableCell>
              </TableRow>
            ) : tokens.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8 text-muted-foreground">
                  No SCIM tokens created yet
                </TableCell>
              </TableRow>
            ) : (
              tokens.map((token) => {
                const status = getTokenStatus(token);
                return (
                  <TableRow key={token.id}>
                    <TableCell>
                      <div>
                        <span className="font-medium">{token.name}</span>
                        {token.description && (
                          <p className="text-xs text-muted-foreground mt-0.5">
                            {token.description}
                          </p>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <code className="text-xs bg-muted px-1.5 py-0.5 rounded">
                        ····{token.token_hint}
                      </code>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          status === 'active'
                            ? 'default'
                            : status === 'expired'
                              ? 'secondary'
                              : 'destructive'
                        }
                      >
                        {status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm">{formatDate(token.created_at)}</TableCell>
                    <TableCell className="text-sm">{formatDate(token.last_used_at)}</TableCell>
                    <TableCell className="text-sm">{formatDate(token.expires_at)}</TableCell>
                    <TableCell>
                      {status === 'active' && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8 text-destructive hover:text-destructive"
                          onClick={() => setRevokeDialog({ open: true, token })}
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

      {/* Create Token Dialog */}
      <Dialog
        open={createDialogOpen}
        onOpenChange={(open) => {
          setCreateDialogOpen(open);
          if (!open) resetForm();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create SCIM Token</DialogTitle>
            <DialogDescription>
              Create a new bearer token for SCIM 2.0 provisioning. The token will only be shown once.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="token-name">Name</Label>
              <Input
                id="token-name"
                placeholder="e.g. Azure AD SCIM"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="token-description">Description (optional)</Label>
              <Input
                id="token-description"
                placeholder="What is this token used for?"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="token-expires">Expires at (optional)</Label>
              <Input
                id="token-expires"
                type="datetime-local"
                value={expiresAt}
                onChange={(e) => setExpiresAt(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={!name.trim() || createMutation.isPending}>
              {createMutation.isPending ? 'Creating...' : 'Create Token'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Token Created Success Dialog */}
      <Dialog
        open={!!createdToken}
        onOpenChange={(open) => {
          if (!open) setCreatedToken(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Key className="h-5 w-5" />
              Token Created
            </DialogTitle>
            <DialogDescription>
              Copy this token now — it will not be shown again.
            </DialogDescription>
          </DialogHeader>
          {createdToken && (
            <div className="space-y-4 py-4">
              <div className="space-y-2">
                <Label>Token</Label>
                <div className="flex items-center gap-2">
                  <code className="flex-1 text-sm bg-muted p-3 rounded-md break-all select-all">
                    {createdToken.token}
                  </code>
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() => handleCopy(createdToken.token)}
                  >
                    {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                  </Button>
                </div>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button onClick={() => setCreatedToken(null)}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Revoke Confirm Dialog */}
      {revokeDialog.token && (
        <ConfirmDialog
          open={revokeDialog.open}
          onOpenChange={(open) => !open && setRevokeDialog({ open: false, token: null })}
          title="Revoke SCIM Token"
          description={`Are you sure you want to revoke "${revokeDialog.token.name}"? Any identity provider using this token will lose access immediately.`}
          confirmLabel="Revoke"
          variant="destructive"
          onConfirm={() => revokeMutation.mutate(revokeDialog.token!.id)}
          isLoading={revokeMutation.isPending}
        />
      )}
    </div>
  );
}
