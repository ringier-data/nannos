import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, Key, Lock, Loader2, Users } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  listSecretsApiV1SecretsGetOptions,
  listSecretsApiV1SecretsGetQueryKey,
  createSecretApiV1SecretsPostMutation,
  deleteSecretApiV1SecretsSecretIdDeleteMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { Secret, SecretType } from '@/api/generated/types.gen';
import { getErrorMessage } from '@/lib/utils';
import { SecretPermissionsDialog } from './SecretPermissionsDialog';

export function SecretsVaultList() {
  const queryClient = useQueryClient();
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showPermissionsDialog, setShowPermissionsDialog] = useState(false);
  const [selectedSecretId, setSelectedSecretId] = useState<number | null>(null);
  const [selectedSecretName, setSelectedSecretName] = useState<string>('');

  // Form state
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [secretType, setSecretType] = useState<SecretType>('foundry_client_secret');
  const [secretValue, setSecretValue] = useState('');

  // Fetch secrets
  const { data: secretsData, isLoading } = useQuery({
    ...listSecretsApiV1SecretsGetOptions(),
  });

  const secrets = secretsData?.items || [];

  // Create mutation
  const createMutation = useMutation({
    ...createSecretApiV1SecretsPostMutation(),
    onSuccess: () => {
      toast.success('Secret created successfully');
      queryClient.invalidateQueries({ queryKey: listSecretsApiV1SecretsGetQueryKey() });
      setShowCreateDialog(false);
      resetForm();
    },
    onError: (err) => {
      toast.error('Failed to create secret', { description: getErrorMessage(err) });
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    ...deleteSecretApiV1SecretsSecretIdDeleteMutation(),
    onSuccess: () => {
      toast.success('Secret deleted successfully');
      queryClient.invalidateQueries({ queryKey: listSecretsApiV1SecretsGetQueryKey() });
      setShowDeleteDialog(false);
      setSelectedSecretId(null);
    },
    onError: (err) => {
      toast.error('Failed to delete secret', { description: getErrorMessage(err) });
    },
  });

  const resetForm = () => {
    setName('');
    setDescription('');
    setSecretType('foundry_client_secret');
    setSecretValue('');
  };

  const handleCreate = () => {
    if (!name.trim()) {
      toast.error('Name is required');
      return;
    }
    if (!secretValue.trim()) {
      toast.error('Secret value is required');
      return;
    }

    createMutation.mutate({
      body: {
        name: name.trim(),
        description: description.trim() || undefined,
        secret_type: secretType,
        secret_value: secretValue.trim(),
      },
    });
  };

  const handleDelete = () => {
    if (selectedSecretId === null) return;

    deleteMutation.mutate({
      path: { secret_id: selectedSecretId },
    });
  };

  const confirmDelete = (secretId: number) => {
    setSelectedSecretId(secretId);
    setShowDeleteDialog(true);
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center p-8">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end">
        <Button onClick={() => setShowCreateDialog(true)}>
          <Plus className="h-4 w-4 mr-2" />
          Create Secret
        </Button>
      </div>

      {secrets.length === 0 ? (
        <div className="text-center py-12 border rounded-lg bg-muted/50">
          <Key className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
          <h3 className="text-lg font-medium mb-2">No secrets yet</h3>
          <p className="text-sm text-muted-foreground mb-4">
            Create a secret to securely store sensitive information like API keys or client secrets.
          </p>
          <Button onClick={() => setShowCreateDialog(true)}>
            <Plus className="h-4 w-4 mr-2" />
            Create Your First Secret
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          {secrets.map((secret: Secret) => (
            <div
              key={secret.id}
              className="flex items-center justify-between p-4 border rounded-lg hover:bg-muted/50 transition-colors"
            >
              <div className="flex items-start gap-3 flex-1 min-w-0">
                <div className="p-2 rounded-md bg-primary/10 flex-shrink-0">
                  <Lock className="h-4 w-4 text-primary" />
                </div>
                <div className="flex-1 min-w-0">
                  <h3 className="font-medium truncate">{secret.name}</h3>
                  {secret.description && (
                    <p className="text-sm text-muted-foreground mt-1 line-clamp-2">
                      {secret.description}
                    </p>
                  )}
                  <div className="flex items-center gap-4 mt-2 text-xs text-muted-foreground">
                    <span className="capitalize">{secret.secret_type.replace('_', ' ')}</span>
                    <span>•</span>
                    <span>Created {secret.created_at ? new Date(secret.created_at).toLocaleDateString() : 'N/A'}</span>
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setSelectedSecretId(secret.id);
                    setSelectedSecretName(secret.name);
                    setShowPermissionsDialog(true);
                  }}
                  title="Manage permissions"
                >
                  <Users className="h-4 w-4" />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => confirmDelete(secret.id)}
                  disabled={deleteMutation.isPending}
                  title="Delete secret"
                >
                  <Trash2 className="h-4 w-4 text-destructive" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create Dialog */}
      <Dialog open={showCreateDialog} onOpenChange={setShowCreateDialog}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Create Secret</DialogTitle>
            <DialogDescription>
              Create a new secret to securely store sensitive information. The secret value will be encrypted and stored in AWS SSM Parameter Store.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="secret-name">Name *</Label>
              <Input
                id="secret-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-foundry-secret"
                disabled={createMutation.isPending}
              />
              <p className="text-xs text-muted-foreground">
                A unique name to identify this secret
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="secret-description">Description (Optional)</Label>
              <Textarea
                id="secret-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Description of what this secret is used for"
                rows={2}
                disabled={createMutation.isPending}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="secret-type">Secret Type *</Label>
              <Select
                value={secretType}
                onValueChange={(value) => setSecretType(value as SecretType)}
                disabled={createMutation.isPending}
              >
                <SelectTrigger id="secret-type">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="foundry_client_secret">Foundry Client Secret</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="secret-value">Secret Value *</Label>
              <Input
                id="secret-value"
                type="password"
                value={secretValue}
                onChange={(e) => setSecretValue(e.target.value)}
                placeholder="Enter the secret value..."
                className="font-mono"
                disabled={createMutation.isPending}
              />
              <p className="text-xs text-muted-foreground flex items-center gap-1">
                <Lock className="h-3 w-3" />
                Encrypted with KMS and stored in AWS SSM Parameter Store
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setShowCreateDialog(false);
                resetForm();
              }}
              disabled={createMutation.isPending}
            >
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={createMutation.isPending}>
              {createMutation.isPending && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Create Secret
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete Secret</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this secret? This action cannot be undone.
              The secret will be removed from AWS SSM Parameter Store and the database.
            </DialogDescription>
          </DialogHeader>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setShowDeleteDialog(false);
                setSelectedSecretId(null);
              }}
              disabled={deleteMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDelete}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Permissions Dialog */}
      {selectedSecretId !== null && (
        <SecretPermissionsDialog
          secretId={selectedSecretId}
          secretName={selectedSecretName}
          open={showPermissionsDialog}
          onOpenChange={setShowPermissionsDialog}
        />
      )}
    </div>
  );
}
