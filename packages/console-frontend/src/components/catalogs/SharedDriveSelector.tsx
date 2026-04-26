import { useState, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  HardDrive,
  Loader2,
  Folder,
  FolderOpen,
  ChevronRight,
  ArrowLeft,
  Check,
  Pencil,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  listSharedDrivesOptions,
  listDriveFoldersOptions,
  updateCatalogMutation,
  getCatalogQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import { getErrorMessage } from '@/lib/utils';

interface SharedDriveSelectorProps {
  catalogId: string;
  /** Current source_config from the catalog, if any. */
  currentConfig?: Record<string, unknown>;
  /** Called after a successful save. */
  onSaved?: () => void;
}

type Step = 'summary' | 'pick-drive' | 'pick-folder';

interface BreadcrumbItem {
  id: string;
  name: string;
}

export function SharedDriveSelector({
  catalogId,
  currentConfig,
  onSaved,
}: SharedDriveSelectorProps) {
  const queryClient = useQueryClient();

  const currentDriveId = currentConfig?.shared_drive_id as string | undefined;
  const currentDriveName = currentConfig?.shared_drive_name as string | undefined;
  const currentFolderId = currentConfig?.folder_id as string | undefined;
  const currentFolderName = currentConfig?.folder_name as string | undefined;
  const hasExisting = !!currentDriveId;

  // Which step are we on?
  const [step, setStep] = useState<Step>(hasExisting ? 'summary' : 'pick-drive');

  // Selected drive (during picking)
  const [selectedDrive, setSelectedDrive] = useState<{
    id: string;
    name: string;
  } | null>(
    currentDriveId && currentDriveName
      ? { id: currentDriveId, name: currentDriveName }
      : null,
  );

  // Folder navigation: breadcrumb trail
  const [folderPath, setFolderPath] = useState<BreadcrumbItem[]>([]);
  // Selected folder (null = drive root)
  const [selectedFolder, setSelectedFolder] = useState<{
    id: string;
    name: string;
  } | null>(
    currentFolderId && currentFolderName
      ? { id: currentFolderId, name: currentFolderName }
      : null,
  );

  // Current parent for folder listing
  const currentParentId =
    folderPath.length > 0 ? folderPath[folderPath.length - 1].id : undefined;

  // --- Queries ---
  const { data: drives, isLoading: drivesLoading } = useQuery({
    ...listSharedDrivesOptions({ query: { catalog_id: catalogId } }),
    enabled: step === 'pick-drive',
  });

  const { data: folders, isLoading: foldersLoading } = useQuery({
    ...listDriveFoldersOptions({
      query: {
        catalog_id: catalogId,
        shared_drive_id: selectedDrive?.id ?? '',
        parent_id: currentParentId ?? null,
      },
    }),
    enabled: step === 'pick-folder' && !!selectedDrive,
  });

  // --- Mutation ---
  const saveMutation = useMutation({
    ...updateCatalogMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: getCatalogQueryKey({ path: { catalog_id: catalogId } }),
      });
      toast.success('Source configuration saved');
      setStep('summary');
      onSaved?.();
    },
    onError: (err) => {
      toast.error('Failed to save configuration', {
        description: getErrorMessage(err),
      });
    },
  });

  // --- Handlers ---
  const handleDriveSelect = useCallback(
    (driveId: string, driveName: string) => {
      setSelectedDrive({ id: driveId, name: driveName });
      setFolderPath([]);
      setSelectedFolder(null);
      setStep('pick-folder');
    },
    [],
  );

  const handleFolderOpen = useCallback(
    (folderId: string, folderName: string) => {
      setFolderPath((prev) => [...prev, { id: folderId, name: folderName }]);
    },
    [],
  );

  const handleBreadcrumbNav = useCallback((index: number) => {
    // Navigate to a breadcrumb position (-1 = drive root)
    setFolderPath((prev) => prev.slice(0, index + 1));
  }, []);

  const handleSelectFolder = useCallback(
    (folderId: string, folderName: string) => {
      setSelectedFolder({ id: folderId, name: folderName });
    },
    [],
  );

  const handleUseRoot = useCallback(() => {
    setSelectedFolder(null);
  }, []);

  const handleSave = useCallback(() => {
    if (!selectedDrive) return;
    const config: Record<string, string> = {
      shared_drive_id: selectedDrive.id,
      shared_drive_name: selectedDrive.name,
    };
    if (selectedFolder) {
      config.folder_id = selectedFolder.id;
      config.folder_name = selectedFolder.name;
    }
    saveMutation.mutate({
      path: { catalog_id: catalogId },
      body: { source_config: config },
    });
  }, [catalogId, selectedDrive, selectedFolder, saveMutation]);

  const handleChange = useCallback(() => {
    setStep('pick-drive');
  }, []);

  // --- Renders ---

  // Summary view: show what's selected
  if (step === 'summary' && hasExisting) {
    return (
      <div className="flex items-center gap-3 rounded-lg border bg-muted/40 px-4 py-3">
        <HardDrive className="h-4 w-4 text-muted-foreground shrink-0" />
        <div className="flex-1 min-w-0 text-sm">
          <span className="font-medium">{currentDriveName ?? 'Unknown drive'}</span>
          {currentFolderName && (
            <>
              <ChevronRight className="inline h-3 w-3 mx-1 text-muted-foreground" />
              <span className="text-muted-foreground">{currentFolderName}</span>
            </>
          )}
          {!currentFolderName && (
            <span className="text-muted-foreground ml-1">(entire drive)</span>
          )}
        </div>
        <Button variant="ghost" size="sm" onClick={handleChange}>
          <Pencil className="mr-1 h-3 w-3" />
          Change
        </Button>
      </div>
    );
  }

  // Drive picker
  if (step === 'pick-drive') {
    if (drivesLoading) {
      return (
        <div className="flex items-center justify-center py-8 text-muted-foreground">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          Loading shared drives...
        </div>
      );
    }
    if (!drives || drives.length === 0) {
      return (
        <div className="text-sm text-muted-foreground py-4">
          No shared drives found. Make sure the connected Google account has
          access to at least one Shared Drive.
        </div>
      );
    }
    return (
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          {hasExisting && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setStep('summary')}
            >
              <ArrowLeft className="h-4 w-4" />
            </Button>
          )}
          <p className="text-sm text-muted-foreground">
            Select a Shared Drive:
          </p>
        </div>
        <div className="grid gap-2">
          {drives.map((drive) => {
            const id = drive.id as string;
            const name = drive.name as string;
            return (
              <Button
                key={id}
                variant="outline"
                className="justify-start h-auto py-3 px-4"
                onClick={() => handleDriveSelect(id, name)}
              >
                <HardDrive className="mr-3 h-4 w-4" />
                {name}
              </Button>
            );
          })}
        </div>
      </div>
    );
  }

  // Folder browser
  if (step === 'pick-folder' && selectedDrive) {
    return (
      <div className="space-y-3">
        {/* Breadcrumb navigation */}
        <div className="flex items-center gap-1 text-sm flex-wrap">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2"
            onClick={() => setStep('pick-drive')}
          >
            <ArrowLeft className="h-3 w-3 mr-1" />
            Drives
          </Button>
          <ChevronRight className="h-3 w-3 text-muted-foreground" />
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2 font-medium"
            onClick={() => {
              setFolderPath([]);
              setSelectedFolder(null);
            }}
          >
            <HardDrive className="h-3 w-3 mr-1" />
            {selectedDrive.name}
          </Button>
          {folderPath.map((item, idx) => (
            <span key={item.id} className="flex items-center gap-1">
              <ChevronRight className="h-3 w-3 text-muted-foreground" />
              <Button
                variant="ghost"
                size="sm"
                className="h-7 px-2"
                onClick={() => handleBreadcrumbNav(idx)}
              >
                {item.name}
              </Button>
            </span>
          ))}
        </div>

        {/* Folder list */}
        {foldersLoading ? (
          <div className="flex items-center justify-center py-6 text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            Loading folders...
          </div>
        ) : (
          <div className="rounded-md border divide-y max-h-64 overflow-y-auto">
            {(folders ?? []).length === 0 ? (
              <div className="text-sm text-muted-foreground py-4 px-4">
                No subfolders here.
              </div>
            ) : (
              (folders ?? []).map((folder) => {
                const fId = folder.id as string;
                const fName = folder.name as string;
                const isSelected = selectedFolder?.id === fId;
                return (
                  <div
                    key={fId}
                    className="flex items-center gap-2 px-3 py-2 hover:bg-muted/50"
                  >
                    {isSelected ? (
                      <FolderOpen className="h-4 w-4 text-primary shrink-0" />
                    ) : (
                      <Folder className="h-4 w-4 text-muted-foreground shrink-0" />
                    )}
                    <button
                      className="flex-1 text-left text-sm truncate hover:underline"
                      onClick={() => handleFolderOpen(fId, fName)}
                    >
                      {fName}
                    </button>
                    <Button
                      variant={isSelected ? 'default' : 'ghost'}
                      size="sm"
                      className="h-7 text-xs shrink-0"
                      onClick={() => handleSelectFolder(fId, fName)}
                    >
                      {isSelected ? (
                        <>
                          <Check className="h-3 w-3 mr-1" />
                          Selected
                        </>
                      ) : (
                        'Select'
                      )}
                    </Button>
                  </div>
                );
              })
            )}
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center justify-between pt-2">
          <div className="text-xs text-muted-foreground">
            {selectedFolder ? (
              <span>
                Syncing from: <strong>{selectedFolder.name}</strong>
              </span>
            ) : (
              <span>Syncing entire drive</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {selectedFolder && (
              <Button variant="ghost" size="sm" onClick={handleUseRoot}>
                Use entire drive
              </Button>
            )}
            <Button
              size="sm"
              onClick={handleSave}
              disabled={saveMutation.isPending}
            >
              {saveMutation.isPending && (
                <Loader2 className="mr-2 h-3 w-3 animate-spin" />
              )}
              Confirm
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return null;
}
