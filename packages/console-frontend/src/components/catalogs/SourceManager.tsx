import { useState, useCallback, useMemo, useRef, useEffect, type KeyboardEvent } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  HardDrive,
  Loader2,
  Folder,
  FolderOpen,
  ChevronRight,
  ChevronDown,
  ArrowLeft,
  Check,
  Plus,
  Trash2,
  Share2,
  Filter,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  listSharedDrivesOptions,
  listDriveFoldersOptions,
  getCatalogQueryKey,
  getCatalogSyncStatusQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import { client } from '@/api/generated/client.gen';
import { getErrorMessage } from '@/lib/utils';

// --- Types (will come from SDK after regen) ---

type CatalogSourceKind = 'shared_drive' | 'drive_folder' | 'shared_folder';

interface CatalogSource {
  id: string;
  type: CatalogSourceKind;
  drive_id?: string | null;
  drive_name?: string | null;
  folder_id?: string | null;
  folder_name?: string | null;
  exclude_folder_patterns?: string[];
}

// --- Common exclusion pattern suggestions ---
const SUGGESTED_PATTERNS = ['archive', 'backup', 'old', 'draft', 'temp', 'deprecated', 'trash'];

// --- Inline API calls (replace with generated SDK after `npm run gen-sdk`) ---

const fetchSources = async (catalogId: string): Promise<CatalogSource[]> => {
  const { data } = await client.get({
    url: '/api/v1/catalogs/{catalog_id}/sources',
    path: { catalog_id: catalogId },
    throwOnError: true,
  });
  return (data ?? []) as CatalogSource[];
};

const addSource = async (catalogId: string, body: Omit<CatalogSource, 'id'>) => {
  const { data } = await client.post({
    url: '/api/v1/catalogs/{catalog_id}/sources',
    path: { catalog_id: catalogId },
    body,
    throwOnError: true,
  });
  return data as CatalogSource;
};

const removeSource = async (catalogId: string, sourceId: string) => {
  await client.delete({
    url: '/api/v1/catalogs/{catalog_id}/sources/{source_id}',
    path: { catalog_id: catalogId, source_id: sourceId },
    throwOnError: true,
  });
};

const updateSource = async (
  catalogId: string,
  sourceId: string,
  body: { exclude_folder_patterns: string[] },
) => {
  const { data } = await client.patch({
    url: '/api/v1/catalogs/{catalog_id}/sources/{source_id}',
    path: { catalog_id: catalogId, source_id: sourceId },
    body,
    throwOnError: true,
  });
  return data as CatalogSource;
};

const fetchSharedFolders = async (catalogId: string): Promise<{ id: string; name: string }[]> => {
  const { data } = await client.get({
    url: '/api/v1/catalogs/shared-folders',
    query: { catalog_id: catalogId },
    throwOnError: true,
  });
  return (data ?? []) as { id: string; name: string }[];
};

const fetchFolderChildren = async (
  catalogId: string,
  parentId: string,
): Promise<{ id: string; name: string }[]> => {
  const { data } = await client.get({
    url: '/api/v1/catalogs/folders',
    query: { catalog_id: catalogId, parent_id: parentId },
    throwOnError: true,
  });
  return (data ?? []) as { id: string; name: string }[];
};

// --- Component ---

interface SourceManagerProps {
  catalogId: string;
  canEdit: boolean;
}

type WizardStep =
  | 'idle'
  | 'pick-type'
  | 'pick-drive'
  | 'pick-folder'         // subfolder within a drive
  | 'pick-shared-folder'  // top-level shared folders
  | 'browse-shared-folder'; // navigate inside a shared folder

interface BreadcrumbItem {
  id: string;
  name: string;
}

// ─────────────────────────────── Exclusion Pattern Editor ───────────────────────────────

function ExclusionPatternEditor({
  patterns,
  onChange,
  disabled,
}: {
  patterns: string[];
  onChange: (patterns: string[]) => void;
  disabled?: boolean;
}) {
  const [inputValue, setInputValue] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const addPattern = useCallback(
    (pattern: string) => {
      const trimmed = pattern.trim().toLowerCase();
      if (!trimmed || patterns.includes(trimmed)) return;
      onChange([...patterns, trimmed]);
      setInputValue('');
    },
    [patterns, onChange],
  );

  const removePattern = useCallback(
    (pattern: string) => {
      onChange(patterns.filter((p) => p !== pattern));
    },
    [patterns, onChange],
  );

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        addPattern(inputValue);
      } else if (e.key === 'Backspace' && !inputValue && patterns.length > 0) {
        removePattern(patterns[patterns.length - 1]);
      }
    },
    [inputValue, patterns, addPattern, removePattern],
  );

  const unusedSuggestions = useMemo(
    () => SUGGESTED_PATTERNS.filter((s) => !patterns.includes(s)),
    [patterns],
  );

  return (
    <div className="space-y-2">
      {/* Chips + input */}
      <div className="flex flex-wrap gap-1.5 items-center rounded-md border border-orange-200 dark:border-orange-700 px-2 py-1.5 bg-white dark:bg-background focus-within:ring-2 focus-within:ring-orange-500/20">
        {patterns.map((p) => (
          <Badge key={p} variant="outline" className="gap-1 text-xs font-normal text-orange-700 border-orange-300 bg-orange-100 dark:text-orange-300 dark:border-orange-700 dark:bg-orange-900/50">
            {p}
            {!disabled && (
              <button
                type="button"
                className="ml-0.5 hover:text-destructive"
                onClick={() => removePattern(p)}
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </Badge>
        ))}
        {!disabled && (
          <Input
            ref={inputRef}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={() => { if (inputValue.trim()) addPattern(inputValue); }}
            className="flex-1 min-w-[100px] border-0 shadow-none h-6 text-xs px-0 focus-visible:ring-0"
            placeholder={patterns.length === 0 ? 'Type a pattern and press Enter...' : 'Add more...'}
          />
        )}
      </div>

      {/* Suggestions */}
      {!disabled && unusedSuggestions.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {unusedSuggestions.map((s) => (
            <button
              key={s}
              type="button"
              className="text-xs text-orange-500 hover:text-orange-700 dark:text-orange-400 dark:hover:text-orange-300 border border-dashed border-orange-300 dark:border-orange-700 rounded-full px-2 py-0.5 transition-colors"
              onClick={() => addPattern(s)}
            >
              + {s}
            </button>
          ))}
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        Folders whose name contains any of these patterns will be <span className="text-orange-600 dark:text-orange-400 font-medium">excluded</span> during sync.
      </p>
    </div>
  );
}

// ─────────────────────────────── Source Manager ───────────────────────────────

export function SourceManager({ catalogId, canEdit }: SourceManagerProps) {
  const queryClient = useQueryClient();
  const sourcesQueryKey = ['catalog-sources', catalogId];

  // --- Wizard state ---
  const [step, setStep] = useState<WizardStep>('idle');
  const [selectedDrive, setSelectedDrive] = useState<{ id: string; name: string } | null>(null);
  const [folderPath, setFolderPath] = useState<BreadcrumbItem[]>([]);
  const [selectedFolder, setSelectedFolder] = useState<{ id: string; name: string } | null>(null);
  // Shared folder browsing
  const [sharedFolderRoot, setSharedFolderRoot] = useState<{ id: string; name: string } | null>(null);
  const [sharedFolderPath, setSharedFolderPath] = useState<BreadcrumbItem[]>([]);
  const [selectedSharedSubfolder, setSelectedSharedSubfolder] = useState<{ id: string; name: string } | null>(null);
  // Exclusion patterns (during add wizard)
  const [wizardExcludePatterns, setWizardExcludePatterns] = useState<string[]>([]);
  // Expanded exclusion editor per source
  const [expandedExclusion, setExpandedExclusion] = useState<string | null>(null);

  const currentDriveFolderParentId = folderPath.length > 0 ? folderPath[folderPath.length - 1].id : undefined;
  const currentSharedFolderParentId =
    sharedFolderPath.length > 0
      ? sharedFolderPath[sharedFolderPath.length - 1].id
      : sharedFolderRoot?.id ?? undefined;

  // --- Queries ---
  const { data: sources = [], isLoading: sourcesLoading } = useQuery({
    queryKey: sourcesQueryKey,
    queryFn: () => fetchSources(catalogId),
  });

  const { data: drives, isLoading: drivesLoading } = useQuery({
    ...listSharedDrivesOptions({ query: { catalog_id: catalogId } }),
    enabled: step === 'pick-drive',
  });

  const { data: driveFolders, isLoading: driveFoldersLoading } = useQuery({
    ...listDriveFoldersOptions({
      query: {
        catalog_id: catalogId,
        shared_drive_id: selectedDrive?.id ?? '',
        parent_id: currentDriveFolderParentId ?? null,
      },
    }),
    enabled: step === 'pick-folder' && !!selectedDrive,
  });

  const { data: sharedFolders, isLoading: sharedFoldersLoading } = useQuery({
    queryKey: ['shared-folders', catalogId],
    queryFn: () => fetchSharedFolders(catalogId),
    enabled: step === 'pick-shared-folder',
  });

  const { data: sharedSubfolders, isLoading: sharedSubfoldersLoading } = useQuery({
    queryKey: ['folder-children', catalogId, currentSharedFolderParentId],
    queryFn: () => fetchFolderChildren(catalogId, currentSharedFolderParentId!),
    enabled: step === 'browse-shared-folder' && !!currentSharedFolderParentId,
  });

  // --- Mutations ---
  const addMutation = useMutation({
    mutationFn: (body: Omit<CatalogSource, 'id'>) => addSource(catalogId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sourcesQueryKey });
      queryClient.invalidateQueries({ queryKey: getCatalogQueryKey({ path: { catalog_id: catalogId } }) });
      toast.success('Source added');
      resetWizard();
    },
    onError: (err) => toast.error('Failed to add source', { description: getErrorMessage(err) }),
  });

  const removeMutation = useMutation({
    mutationFn: (sourceId: string) => removeSource(catalogId, sourceId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sourcesQueryKey });
      queryClient.invalidateQueries({ queryKey: getCatalogQueryKey({ path: { catalog_id: catalogId } }) });
      toast.success('Source removed');
    },
    onError: (err) => toast.error('Failed to remove source', { description: getErrorMessage(err) }),
  });

  const updateExclusionsMutation = useMutation({
    mutationFn: ({ sourceId, patterns }: { sourceId: string; patterns: string[] }) =>
      updateSource(catalogId, sourceId, { exclude_folder_patterns: patterns }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sourcesQueryKey });
      queryClient.invalidateQueries({ queryKey: getCatalogSyncStatusQueryKey({ path: { catalog_id: catalogId } }) });
      queryClient.invalidateQueries({ queryKey: getCatalogQueryKey({ path: { catalog_id: catalogId } }) });
      toast.success('Exclusion patterns updated — syncing...');
    },
    onError: (err) => toast.error('Failed to update exclusions', { description: getErrorMessage(err) }),
  });

  // Local state that mirrors server patterns for optimistic UI
  const [localPatterns, setLocalPatterns] = useState<Record<string, string[]>>({});
  useEffect(() => {
    const map: Record<string, string[]> = {};
    for (const s of sources) {
      map[s.id] = s.exclude_folder_patterns ?? [];
    }
    setLocalPatterns(map);
  }, [sources]);

  // --- Handlers ---
  const resetWizard = useCallback(() => {
    setStep('idle');
    setSelectedDrive(null);
    setFolderPath([]);
    setSelectedFolder(null);
    setSharedFolderRoot(null);
    setSharedFolderPath([]);
    setSelectedSharedSubfolder(null);
    setWizardExcludePatterns([]);
  }, []);

  const handleDriveSelect = useCallback((driveId: string, driveName: string) => {
    setSelectedDrive({ id: driveId, name: driveName });
    setFolderPath([]);
    setSelectedFolder(null);
    setStep('pick-folder');
  }, []);

  const handleDriveFolderOpen = useCallback((folderId: string, folderName: string) => {
    setFolderPath((prev) => [...prev, { id: folderId, name: folderName }]);
    setSelectedFolder(null);
  }, []);

  const handleDriveBreadcrumbNav = useCallback((index: number) => {
    setFolderPath((prev) => prev.slice(0, index + 1));
    setSelectedFolder(null);
  }, []);

  const handleSelectDriveFolder = useCallback((folderId: string, folderName: string) => {
    setSelectedFolder((prev) => (prev?.id === folderId ? null : { id: folderId, name: folderName }));
  }, []);

  // Shared folder browsing
  const handleSharedFolderSelect = useCallback((folderId: string, folderName: string) => {
    setSharedFolderRoot({ id: folderId, name: folderName });
    setSharedFolderPath([]);
    setSelectedSharedSubfolder(null);
    setStep('browse-shared-folder');
  }, []);

  const handleSharedSubfolderOpen = useCallback((folderId: string, folderName: string) => {
    setSharedFolderPath((prev) => [...prev, { id: folderId, name: folderName }]);
    setSelectedSharedSubfolder(null);
  }, []);

  const handleSharedBreadcrumbNav = useCallback((index: number) => {
    setSharedFolderPath((prev) => prev.slice(0, index + 1));
    setSelectedSharedSubfolder(null);
  }, []);

  const handleSelectSharedSubfolder = useCallback((folderId: string, folderName: string) => {
    setSelectedSharedSubfolder((prev) =>
      prev?.id === folderId ? null : { id: folderId, name: folderName },
    );
  }, []);

  const handleSaveDriveSource = useCallback(() => {
    if (!selectedDrive) return;
    const base: Omit<CatalogSource, 'id'> = selectedFolder
      ? {
          type: 'drive_folder',
          drive_id: selectedDrive.id,
          drive_name: selectedDrive.name,
          folder_id: selectedFolder.id,
          folder_name: selectedFolder.name,
        }
      : {
          type: 'shared_drive',
          drive_id: selectedDrive.id,
          drive_name: selectedDrive.name,
        };
    if (wizardExcludePatterns.length > 0) {
      base.exclude_folder_patterns = wizardExcludePatterns;
    }
    addMutation.mutate(base);
  }, [selectedDrive, selectedFolder, wizardExcludePatterns, addMutation]);

  const handleSaveSharedFolder = useCallback(() => {
    if (!sharedFolderRoot) return;
    const target = selectedSharedSubfolder ?? sharedFolderRoot;
    const base: Omit<CatalogSource, 'id'> = {
      type: 'shared_folder',
      folder_id: target.id,
      folder_name: target.name,
    };
    if (wizardExcludePatterns.length > 0) {
      base.exclude_folder_patterns = wizardExcludePatterns;
    }
    addMutation.mutate(base);
  }, [sharedFolderRoot, selectedSharedSubfolder, wizardExcludePatterns, addMutation]);

  const handleUpdateExclusions = useCallback(
    (sourceId: string, patterns: string[]) => {
      setLocalPatterns((prev) => ({ ...prev, [sourceId]: patterns }));
      updateExclusionsMutation.mutate({ sourceId, patterns });
    },
    [updateExclusionsMutation],
  );

  // --- Source label/icon helpers ---
  const sourceLabel = (s: CatalogSource) => {
    if (s.type === 'shared_drive') return s.drive_name ?? 'Shared Drive';
    if (s.type === 'drive_folder')
      return `${s.drive_name ?? 'Drive'} / ${s.folder_name ?? 'Folder'}`;
    return s.folder_name ?? 'Shared Folder';
  };

  const sourceIcon = (s: CatalogSource) => {
    if (s.type === 'shared_folder') return <Share2 className="h-4 w-4 text-muted-foreground shrink-0" />;
    if (s.type === 'drive_folder') return <Folder className="h-4 w-4 text-muted-foreground shrink-0" />;
    return <HardDrive className="h-4 w-4 text-muted-foreground shrink-0" />;
  };

  // --- Renders ---

  if (sourcesLoading) {
    return (
      <div className="flex items-center justify-center py-4 text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading sources...
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* ─── Sources list ─── */}
      {sources.length > 0 && step === 'idle' && (
        <div className="rounded-md border divide-y">
          {sources.map((s) => {
            const exclusions = localPatterns[s.id] ?? s.exclude_folder_patterns ?? [];
            const isExpanded = expandedExclusion === s.id;

            return (
              <div key={s.id} className="px-4 py-2.5">
                <div className="flex items-center gap-3">
                  {sourceIcon(s)}
                  <span className="flex-1 text-sm truncate">{sourceLabel(s)}</span>

                  {/* Exclusion toggle — single stable element regardless of pattern count */}
                  {canEdit && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className={`h-7 w-7 relative ${isExpanded ? 'bg-orange-100 text-orange-600 dark:bg-orange-950/40 dark:text-orange-400' : 'text-muted-foreground hover:text-foreground'}`}
                          onClick={() => setExpandedExclusion(isExpanded ? null : s.id)}
                        >
                          <Filter className="h-3.5 w-3.5" />
                          {exclusions.length > 0 && (
                            <span className="absolute -top-1 -right-1 h-4 min-w-4 rounded-full bg-orange-500 text-white text-[10px] font-medium flex items-center justify-center px-0.5">
                              {exclusions.length}
                            </span>
                          )}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        {exclusions.length > 0
                          ? `${exclusions.length} exclusion pattern${exclusions.length > 1 ? 's' : ''}`
                          : 'Exclude folders by pattern'}
                      </TooltipContent>
                    </Tooltip>
                  )}

                  <span className="text-xs text-muted-foreground capitalize">
                    {s.type.replace('_', ' ')}
                  </span>

                  {canEdit && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-destructive hover:text-destructive"
                      onClick={() => removeMutation.mutate(s.id)}
                      disabled={removeMutation.isPending}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  )}
                </div>

                {/* Expanded exclusion editor */}
                {canEdit && isExpanded && (
                  <div className="mt-3 ml-7 rounded-md border border-orange-200 dark:border-orange-800/50 bg-orange-50/50 dark:bg-orange-950/20 p-3">
                    <p className="text-xs font-medium text-orange-700 dark:text-orange-400 mb-2">
                      Excluding folders matching:
                    </p>
                    <ExclusionPatternEditor
                      patterns={exclusions}
                      onChange={(newPatterns) => handleUpdateExclusions(s.id, newPatterns)}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* ─── Add source button ─── */}
      {step === 'idle' && canEdit && (
        <Button variant="outline" size="sm" onClick={() => setStep('pick-type')}>
          <Plus className="mr-2 h-4 w-4" /> Add Source
        </Button>
      )}

      {/* ─── Step: pick source type ─── */}
      {step === 'pick-type' && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={resetWizard}>
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <p className="text-sm text-muted-foreground">Choose source type:</p>
          </div>
          <div className="grid gap-2">
            <Button
              variant="outline"
              className="justify-start h-auto py-3 px-4"
              onClick={() => setStep('pick-drive')}
            >
              <HardDrive className="mr-3 h-4 w-4" />
              Shared Drive
              <span className="ml-auto text-xs text-muted-foreground">From team drives</span>
            </Button>
            <Button
              variant="outline"
              className="justify-start h-auto py-3 px-4"
              onClick={() => setStep('pick-shared-folder')}
            >
              <Share2 className="mr-3 h-4 w-4" />
              Shared Folder
              <span className="ml-auto text-xs text-muted-foreground">From Shared with me</span>
            </Button>
          </div>
        </div>
      )}

      {/* ─── Step: pick shared drive ─── */}
      {step === 'pick-drive' && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => setStep('pick-type')}>
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <p className="text-sm text-muted-foreground">Select a Shared Drive:</p>
          </div>
          {drivesLoading ? (
            <div className="flex items-center justify-center py-6 text-muted-foreground">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading drives...
            </div>
          ) : !drives || drives.length === 0 ? (
            <div className="text-sm text-muted-foreground py-4">No shared drives found.</div>
          ) : (
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
          )}
        </div>
      )}

      {/* ─── Step: browse folders within a drive ─── */}
      {step === 'pick-folder' && selectedDrive && (
        <FolderBrowserPanel
          backLabel="Drives"
          onBack={() => setStep('pick-drive')}
          rootIcon={<HardDrive className="h-3 w-3 mr-1" />}
          rootLabel={selectedDrive.name}
          onRootClick={() => { setFolderPath([]); setSelectedFolder(null); }}
          breadcrumb={folderPath}
          onBreadcrumbNav={handleDriveBreadcrumbNav}
          folders={driveFolders as { id: string; name: string }[] | undefined}
          foldersLoading={driveFoldersLoading}
          selectedFolder={selectedFolder}
          onFolderOpen={handleDriveFolderOpen}
          onFolderSelect={handleSelectDriveFolder}
          summaryText={
            selectedFolder
              ? (<span>Adding: <strong>{selectedDrive.name}</strong> / <strong>{selectedFolder.name}</strong></span>)
              : (<span>Adding entire drive: <strong>{selectedDrive.name}</strong></span>)
          }
          excludePatterns={wizardExcludePatterns}
          onExcludePatternsChange={setWizardExcludePatterns}
          onSave={handleSaveDriveSource}
          onCancel={resetWizard}
          saving={addMutation.isPending}
        />
      )}

      {/* ─── Step: pick top-level shared folder ─── */}
      {step === 'pick-shared-folder' && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => setStep('pick-type')}>
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <p className="text-sm text-muted-foreground">Select a shared folder to browse:</p>
          </div>
          {sharedFoldersLoading ? (
            <div className="flex items-center justify-center py-6 text-muted-foreground">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading shared folders...
            </div>
          ) : !sharedFolders || sharedFolders.length === 0 ? (
            <div className="text-sm text-muted-foreground py-4">
              No shared folders found in &quot;Shared with me&quot;.
            </div>
          ) : (
            <div className="rounded-md border divide-y max-h-64 overflow-y-auto">
              {sharedFolders.map((folder) => (
                <div key={folder.id} className="flex items-center gap-2 px-3 py-2 hover:bg-muted/50">
                  <Share2 className="h-4 w-4 text-muted-foreground shrink-0" />
                  <button
                    className="flex-1 text-left text-sm truncate hover:underline"
                    onClick={() => handleSharedFolderSelect(folder.id, folder.name)}
                  >
                    {folder.name}
                  </button>
                  <ChevronRight className="h-4 w-4 text-muted-foreground" />
                </div>
              ))}
            </div>
          )}
          <div className="flex justify-end">
            <Button variant="ghost" size="sm" onClick={resetWizard}>Cancel</Button>
          </div>
        </div>
      )}

      {/* ─── Step: browse subfolders within a shared folder ─── */}
      {step === 'browse-shared-folder' && sharedFolderRoot && (
        <FolderBrowserPanel
          backLabel="Shared folders"
          onBack={() => { setSharedFolderRoot(null); setSharedFolderPath([]); setStep('pick-shared-folder'); }}
          rootIcon={<Share2 className="h-3 w-3 mr-1" />}
          rootLabel={sharedFolderRoot.name}
          onRootClick={() => { setSharedFolderPath([]); setSelectedSharedSubfolder(null); }}
          breadcrumb={sharedFolderPath}
          onBreadcrumbNav={handleSharedBreadcrumbNav}
          folders={sharedSubfolders}
          foldersLoading={sharedSubfoldersLoading}
          selectedFolder={selectedSharedSubfolder}
          onFolderOpen={handleSharedSubfolderOpen}
          onFolderSelect={handleSelectSharedSubfolder}
          summaryText={
            selectedSharedSubfolder
              ? (<span>Adding: <strong>{selectedSharedSubfolder.name}</strong></span>)
              : (<span>Adding: <strong>{sharedFolderRoot.name}</strong> (entire folder)</span>)
          }
          excludePatterns={wizardExcludePatterns}
          onExcludePatternsChange={setWizardExcludePatterns}
          onSave={handleSaveSharedFolder}
          onCancel={resetWizard}
          saving={addMutation.isPending}
        />
      )}

      {/* ─── Empty state ─── */}
      {sources.length === 0 && step === 'idle' && (
        <div className="text-sm text-muted-foreground">
          No sources configured. Add a Shared Drive or shared folder to start syncing.
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────── Folder Browser Panel ───────────────────────────────

interface FolderBrowserPanelProps {
  backLabel: string;
  onBack: () => void;
  rootIcon: React.ReactNode;
  rootLabel: string;
  onRootClick: () => void;
  breadcrumb: BreadcrumbItem[];
  onBreadcrumbNav: (index: number) => void;
  folders: { id: string; name: string }[] | undefined;
  foldersLoading: boolean;
  selectedFolder: { id: string; name: string } | null;
  onFolderOpen: (id: string, name: string) => void;
  onFolderSelect: (id: string, name: string) => void;
  summaryText: React.ReactNode;
  excludePatterns: string[];
  onExcludePatternsChange: (patterns: string[]) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
}

function FolderBrowserPanel({
  backLabel,
  onBack,
  rootIcon,
  rootLabel,
  onRootClick,
  breadcrumb,
  onBreadcrumbNav,
  folders,
  foldersLoading,
  selectedFolder,
  onFolderOpen,
  onFolderSelect,
  summaryText,
  excludePatterns,
  onExcludePatternsChange,
  onSave,
  onCancel,
  saving,
}: FolderBrowserPanelProps) {
  const [showExclusions, setShowExclusions] = useState(false);

  return (
    <div className="space-y-3">
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 text-sm flex-wrap">
        <Button variant="ghost" size="sm" className="h-7 px-2" onClick={onBack}>
          <ArrowLeft className="h-3 w-3 mr-1" /> {backLabel}
        </Button>
        <ChevronRight className="h-3 w-3 text-muted-foreground" />
        <Button variant="ghost" size="sm" className="h-7 px-2 font-medium" onClick={onRootClick}>
          {rootIcon} {rootLabel}
        </Button>
        {breadcrumb.map((item, idx) => (
          <span key={item.id} className="flex items-center gap-1">
            <ChevronRight className="h-3 w-3 text-muted-foreground" />
            <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => onBreadcrumbNav(idx)}>
              {item.name}
            </Button>
          </span>
        ))}
      </div>

      {/* Folder list */}
      {foldersLoading ? (
        <div className="flex items-center justify-center py-6 text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading folders...
        </div>
      ) : (
        <div className="rounded-md border divide-y max-h-64 overflow-y-auto">
          {(folders ?? []).length === 0 ? (
            <div className="text-sm text-muted-foreground py-4 px-4">No subfolders here.</div>
          ) : (
            (folders ?? []).map((folder) => {
              const isSelected = selectedFolder?.id === folder.id;
              return (
                <div key={folder.id} className="flex items-center gap-2 px-3 py-2 hover:bg-muted/50">
                  {isSelected ? (
                    <FolderOpen className="h-4 w-4 text-primary shrink-0" />
                  ) : (
                    <Folder className="h-4 w-4 text-muted-foreground shrink-0" />
                  )}
                  <button
                    className="flex-1 text-left text-sm truncate hover:underline"
                    onClick={() => onFolderOpen(folder.id, folder.name)}
                  >
                    {folder.name}
                  </button>
                  <Button
                    variant={isSelected ? 'default' : 'ghost'}
                    size="sm"
                    className="h-7 text-xs shrink-0"
                    onClick={() => onFolderSelect(folder.id, folder.name)}
                  >
                    {isSelected ? (
                      <><Check className="h-3 w-3 mr-1" /> Selected</>
                    ) : 'Select'}
                  </Button>
                </div>
              );
            })
          )}
        </div>
      )}

      {/* Exclusion patterns toggle */}
      <button
        type="button"
        className="flex items-center gap-1.5 text-xs text-orange-600 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300 transition-colors"
        onClick={() => setShowExclusions(!showExclusions)}
      >
        {showExclusions ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <Filter className="h-3 w-3" />
        Exclude folders by pattern
        {excludePatterns.length > 0 && (
          <Badge variant="outline" className="text-xs h-4 px-1.5 text-orange-600 border-orange-300 dark:text-orange-400 dark:border-orange-700">{excludePatterns.length}</Badge>
        )}
      </button>
      {showExclusions && (
        <div className="ml-5 rounded-md border border-orange-200 dark:border-orange-800/50 bg-orange-50/50 dark:bg-orange-950/20 p-3">
          <ExclusionPatternEditor
            patterns={excludePatterns}
            onChange={onExcludePatternsChange}
          />
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center justify-between pt-2">
        <div className="text-xs text-muted-foreground">{summaryText}</div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
          <Button size="sm" onClick={onSave} disabled={saving}>
            {saving && <Loader2 className="mr-2 h-3 w-3 animate-spin" />}
            Add
          </Button>
        </div>
      </div>
    </div>
  );
}
