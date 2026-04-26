import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  ArrowLeft,
  Settings2,
  RefreshCw,
  Trash2,
  Shield,
  AlertCircle,
  Link2,
  SearchCheck,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
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
  getCatalogOptions,
  getCatalogSyncStatusOptions,
  deleteCatalogMutation,
  triggerCatalogSyncMutation,
  pauseCatalogSyncMutation,
  resumeCatalogSyncMutation,
  cancelCatalogSyncMutation,
  reindexCatalogMutation,
  listCatalogsQueryKey,
  getCatalogSyncStatusQueryKey,
  listCatalogFilesQueryKey,
  getCatalogQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import { CatalogFileBrowser } from '@/components/catalogs/CatalogFileBrowser';
import { SyncStatusBar } from '@/components/catalogs/SyncStatusBar';
import { EditCatalogDialog } from '@/components/catalogs/EditCatalogDialog';
import { CatalogPermissionsDialog } from '@/components/catalogs/CatalogPermissionsDialog';
import { GoogleDriveConnect } from '@/components/catalogs/GoogleDriveConnect';
import { SourceManager } from '@/components/catalogs/SourceManager';
import { useAuth } from '@/contexts/AuthContext';
import { useSocket } from '@/components/chat/contexts/SocketContext';
import { getErrorMessage } from '@/lib/utils';

export function CatalogDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user, adminMode } = useAuth();
  const { onCatalogSyncProgress } = useSocket();

  const [showEditDialog, setShowEditDialog] = useState(false);
  const [showPermissionsDialog, setShowPermissionsDialog] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  const { data: catalog, isLoading } = useQuery({
    ...getCatalogOptions({ path: { catalog_id: id! } }),
    enabled: !!id,
  });

  const reindexMutation = useMutation({
    ...reindexCatalogMutation(),
    onSuccess: () => {
      toast.success('Re-indexing started');
      queryClient.invalidateQueries({ queryKey: getCatalogSyncStatusQueryKey({ path: { catalog_id: id! } }) });
    },
    onError: (err) => { toast.error('Re-index failed', { description: getErrorMessage(err) }); },
  });

  const { data: syncStatus } = useQuery({
    ...getCatalogSyncStatusOptions({ path: { catalog_id: id! } }),
    enabled: !!id,
  });

  const syncActive = syncStatus?.status != null &&
    ['running', 'pending', 'paused', 'cancelling', 'reindexing'].includes(syncStatus.status);

  // Push-based sync status updates via Socket.IO
  useEffect(() => {
    const syncStatusKey = getCatalogSyncStatusQueryKey({ path: { catalog_id: id! } });
    const filesKey = listCatalogFilesQueryKey({ path: { catalog_id: id! } });
    const catalogKey = getCatalogQueryKey({ path: { catalog_id: id! } });
    return onCatalogSyncProgress((data) => {
      if (data.catalog_id !== id) return;
      // Merge incremental fields into the cached sync status
      queryClient.setQueryData(syncStatusKey, (prev: typeof syncStatus) => {
        if (!prev) return prev;
        const updated = { ...prev };
        for (const [k, v] of Object.entries(data)) {
          if (k !== 'catalog_id' && k !== 'job_id' && v !== undefined) {
            (updated as Record<string, unknown>)[k] = v;
          }
        }
        return updated;
      });
      // On terminal states, refresh file list and catalog
      const terminalStates = ['completed', 'failed', 'cancelled'];
      if (typeof data.status === 'string' && terminalStates.includes(data.status)) {
        queryClient.invalidateQueries({ queryKey: filesKey });
        queryClient.invalidateQueries({ queryKey: catalogKey });
        // Also refetch the full sync status to get complete data
        queryClient.invalidateQueries({ queryKey: syncStatusKey });
      }
      // When total_files arrives, bulk registration is complete — refresh file list immediately
      if (typeof data.total_files === 'number' && data.total_files > 0) {
        queryClient.invalidateQueries({ queryKey: filesKey });
      }
    });
  }, [id, onCatalogSyncProgress, queryClient]);

  const invalidateSyncStatus = () => {
    queryClient.invalidateQueries({ queryKey: getCatalogSyncStatusQueryKey({ path: { catalog_id: id! } }) });
  };

  const syncMutation = useMutation({
    ...triggerCatalogSyncMutation(),
    onSuccess: () => {
      toast.success('Sync started');
      invalidateSyncStatus();
      queryClient.invalidateQueries({ queryKey: getCatalogQueryKey({ path: { catalog_id: id! } }) });
    },
    onError: (err) => {
      toast.error('Failed to start sync', { description: getErrorMessage(err) });
    },
  });

  const pauseMutation = useMutation({
    ...pauseCatalogSyncMutation(),
    onSuccess: () => { toast.success('Sync paused'); invalidateSyncStatus(); },
    onError: (err) => { toast.error('Failed to pause sync', { description: getErrorMessage(err) }); },
  });

  const resumeMutation = useMutation({
    ...resumeCatalogSyncMutation(),
    onSuccess: () => { toast.success('Sync resumed'); invalidateSyncStatus(); },
    onError: (err) => { toast.error('Failed to resume sync', { description: getErrorMessage(err) }); },
  });

  const cancelMutation = useMutation({
    ...cancelCatalogSyncMutation(),
    onSuccess: () => { toast.success('Sync cancelled'); invalidateSyncStatus(); },
    onError: (err) => { toast.error('Failed to cancel sync', { description: getErrorMessage(err) }); },
  });

  const deleteMut = useMutation({
    ...deleteCatalogMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: listCatalogsQueryKey() });
      toast.success('Catalog deleted');
      navigate('/app/catalogs');
    },
    onError: (err) => {
      toast.error('Failed to delete catalog', { description: getErrorMessage(err) });
    },
  });

  const isOwner = catalog?.owner_user_id === user?.id;
  const isAdmin = user?.is_administrator && adminMode;
  const canEdit = !!(isOwner || isAdmin);
  const syncTerminalWithIssues = syncStatus?.status != null &&
    (syncStatus.status === 'failed' ||
     syncStatus.status === 'cancelled' ||
     (syncStatus.status === 'completed' && (syncStatus.failed_files ?? 0) > 0));
  const showSyncBar = syncActive || syncTerminalWithIssues;
  const isSyncing = syncActive;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!catalog) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4">
        <AlertCircle className="h-12 w-12 text-muted-foreground" />
        <p className="text-muted-foreground">Catalog not found</p>
        <Button variant="outline" onClick={() => navigate('/app/catalogs')}>
          Back to Catalogs
        </Button>
      </div>
    );
  }

  const hasConnection = catalog.has_connection;
  const hasDriveSelected = !!catalog.source_config?.shared_drive_id || !!(catalog.source_config?.sources as unknown[] | undefined)?.length;
  const isReady = hasConnection && hasDriveSelected;

  const totalPages = catalog.total_pages ?? 0;
  const indexedPages = catalog.indexed_pages ?? 0;
  const hasUnindexed = totalPages > 0 && indexedPages < totalPages;
  const isReindexing = reindexMutation.isPending || syncStatus?.status === 'reindexing';

  return (
    <div className="flex flex-col gap-6 p-4">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={() => navigate('/app/catalogs')}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight truncate">{catalog.name}</h1>
            <Badge
              variant={catalog.status === 'active' ? 'default' : catalog.status === 'error' ? 'destructive' : 'secondary'}
              className={catalog.status === 'active' ? 'bg-green-600' : ''}
            >
              {catalog.status}
            </Badge>
          </div>
          {catalog.description && (
            <p className="text-muted-foreground text-sm mt-1">{catalog.description}</p>
          )}
        </div>
        {canEdit && (
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowPermissionsDialog(true)}
            >
              <Shield className="mr-2 h-4 w-4" />
              Permissions
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowEditDialog(true)}
            >
              <Settings2 className="mr-2 h-4 w-4" />
              Edit
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!isReady || isSyncing}
              onClick={() => syncMutation.mutate({ path: { catalog_id: id! } })}
            >
              <RefreshCw className={`mr-2 h-4 w-4 ${isSyncing ? 'animate-spin' : ''}`} />
              {isSyncing ? 'Syncing...' : 'Sync'}
            </Button>
            {hasUnindexed && (
              <Button
                variant="outline"
                size="sm"
                disabled={isSyncing || isReindexing}
                onClick={() => reindexMutation.mutate({ path: { catalog_id: id! } })}
              >
                <SearchCheck className={`mr-2 h-4 w-4 ${isReindexing ? 'animate-spin' : ''}`} />
                {isReindexing ? 'Re-indexing...' : `Re-index (${totalPages - indexedPages})`}
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive hover:text-destructive"
              onClick={() => setShowDeleteDialog(true)}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        )}
      </div>

      {/* Google Drive Connection */}
      {canEdit && !hasConnection && (
        <div className="rounded-lg border border-dashed p-6 text-center">
          <Link2 className="mx-auto h-10 w-10 text-muted-foreground mb-3" />
          <h3 className="font-medium mb-1">Connect Google Drive</h3>
          <p className="text-sm text-muted-foreground mb-4">
            Connect a Google Shared Drive to start syncing documents.
          </p>
          <GoogleDriveConnect catalogId={id!} />
        </div>
      )}

      {/* Source configuration: multi-source management */}
      {hasConnection && (
        <div className="rounded-lg border p-4">
          <h3 className="text-sm font-medium mb-2">Sources</h3>
          <SourceManager catalogId={id!} canEdit={canEdit} />
        </div>
      )}

      {/* Sync Status */}
      {syncStatus && showSyncBar && (
        <SyncStatusBar
          syncJob={syncStatus}
          onPause={() => pauseMutation.mutate({ path: { catalog_id: id! } })}
          onResume={() => resumeMutation.mutate({ path: { catalog_id: id! } })}
          onCancel={() => cancelMutation.mutate({ path: { catalog_id: id! } })}
        />
      )}

      {/* File Browser */}
      {isReady && (
        <CatalogFileBrowser catalogId={id!} canEdit={canEdit} isReindexing={isReindexing} />
      )}

      {/* Info footer */}
      <div className="flex items-center gap-4 text-xs text-muted-foreground">
        {catalog.last_synced_at && (
          <span>Last synced: {new Date(catalog.last_synced_at).toLocaleString()}</span>
        )}
        {catalog.owner && <span>Owner: {catalog.owner.name}</span>}
      </div>

      {/* Edit Dialog */}
      {showEditDialog && (
        <EditCatalogDialog
          catalog={catalog}
          open={showEditDialog}
          onOpenChange={setShowEditDialog}
        />
      )}

      {/* Permissions Dialog */}
      {showPermissionsDialog && (
        <CatalogPermissionsDialog
          catalogId={id!}
          open={showPermissionsDialog}
          onOpenChange={setShowPermissionsDialog}
        />
      )}

      {/* Delete Confirmation */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Catalog</AlertDialogTitle>
            <AlertDialogDescription>
              This will permanently delete &quot;{catalog.name}&quot; and all its indexed content.
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => deleteMut.mutate({ path: { catalog_id: id! } })}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
