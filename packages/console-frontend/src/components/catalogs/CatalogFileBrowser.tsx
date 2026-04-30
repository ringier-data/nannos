import { useState, useEffect } from 'react';
import {
  FileText,
  ChevronRight,
  ChevronDown,
  ChevronLeft,
  FolderOpen,
  Check,
  Search,
  ExternalLink,
  Ban,
  FileIcon,
  Calendar,
  Hash,
  Info,
  Loader2,
  Clock,
  AlertCircle,
  SkipForward,
  X,
} from 'lucide-react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  listCatalogFilesOptions,
  listFilePagesOptions,
  updateFileIndexingMutation,
  listCatalogFilesQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import type { CatalogFile, CatalogFileSyncStatus, CatalogPage } from '@/api/generated/types.gen';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { getErrorMessage } from '@/lib/utils';

const PAGE_SIZE = 50;

const STATUS_OPTIONS: { value: CatalogFileSyncStatus; label: string; icon: typeof Check }[] = [
  { value: 'pending', label: 'Queued', icon: Clock },
  { value: 'syncing', label: 'Syncing', icon: Loader2 },
  { value: 'synced', label: 'Synced', icon: Check },
  { value: 'failed', label: 'Failed', icon: AlertCircle },
  { value: 'skipped', label: 'Skipped', icon: SkipForward },
];

interface CatalogFileBrowserProps {
  catalogId: string;
  canEdit?: boolean;
  isReindexing?: boolean;
}

function driveUrl(sourceFileId: string): string {
  return `https://drive.google.com/file/d/${sourceFileId}`;
}

function mimeIcon(mimeType: string | null | undefined, className = 'h-4 w-4 shrink-0') {
  if (mimeType?.includes('presentation') || mimeType?.includes('google-apps.presentation')) {
    return <FileText className={`${className} text-orange-500`} />;
  }
  if (mimeType?.includes('pdf')) {
    return <FileText className={`${className} text-red-500`} />;
  }
  if (mimeType?.includes('document') || mimeType?.includes('google-apps.document')) {
    return <FileText className={`${className} text-blue-500`} />;
  }
  if (mimeType?.includes('spreadsheet') || mimeType?.includes('google-apps.spreadsheet')) {
    return <FileText className={`${className} text-green-500`} />;
  }
  return <FileText className={`${className} text-muted-foreground`} />;
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '—';
  return new Date(dateStr).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function CatalogFileBrowser({ catalogId, canEdit, isReindexing }: CatalogFileBrowserProps) {
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<CatalogFileSyncStatus | null>(null);

  // Debounce search input
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(search);
      setPage(0);
      setSelectedFileId(null);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const { data: filesData, isLoading } = useQuery({
    ...listCatalogFilesOptions({
      path: { catalog_id: catalogId },
      query: {
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        ...(debouncedSearch ? { search: debouncedSearch } : {}),
        ...(statusFilter ? { status: statusFilter } : {}),
      },
    }),
  });

  const files = filesData?.items ?? [];
  const totalFiles = filesData?.total ?? 0;
  const totalPageCount = Math.ceil(totalFiles / PAGE_SIZE);

  // Group files by folder
  const folders = new Map<string, CatalogFile[]>();
  for (const file of files) {
    const folder = file.folder_path || '/';
    if (!folders.has(folder)) folders.set(folder, []);
    folders.get(folder)!.push(file);
  }
  const sortedFolders = Array.from(folders.entries()).sort(([a], [b]) => a.localeCompare(b));

  if (!isLoading && totalFiles === 0 && !debouncedSearch && !statusFilter) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <p className="text-muted-foreground">
          No files yet. Click Sync to start indexing documents.
        </p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
      {/* File list */}
      <div className="lg:col-span-1 border rounded-lg overflow-hidden">
        <div className="p-3 border-b bg-muted/50 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">Files ({totalFiles.toLocaleString()})</h3>
          </div>
          <div className="relative">
            <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
            <Input
              placeholder="Search files..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 pl-8 pr-8 text-sm"
            />
            {search && (
              <button
                onClick={() => setSearch('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          {/* Status filter pills */}
          <div className="flex items-center gap-1 flex-wrap">
            {STATUS_OPTIONS.map(({ value, label, icon: Icon }) => (
              <button
                key={value}
                onClick={() => {
                  setStatusFilter(prev => prev === value ? null : value);
                  setPage(0);
                  setSelectedFileId(null);
                }}
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border transition-colors ${
                  statusFilter === value
                    ? 'bg-primary text-primary-foreground border-primary'
                    : 'bg-background text-muted-foreground border-input hover:bg-accent'
                }`}
              >
                <Icon className={`h-3 w-3 ${value === 'syncing' && statusFilter === value ? 'animate-spin' : ''}`} />
                {label}
              </button>
            ))}
          </div>
        </div>
        <ScrollArea className="h-[calc(100vh-20rem)] [&_[data-slot=scroll-area-viewport]]:!overflow-x-hidden [&_[data-slot=scroll-area-viewport]>div]:!block">
          {isLoading ? (
            <div className="flex items-center justify-center h-32">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : files.length === 0 ? (
            <div className="flex items-center justify-center h-32 text-sm text-muted-foreground">
              {debouncedSearch || statusFilter ? 'No files match your filters' : 'No files'}
            </div>
          ) : (
          <div className="p-2 overflow-hidden">
            {sortedFolders.map(([folder, folderFiles]) => (
              <div key={folder} className="mb-2">
                {folder !== '/' && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <div className="flex items-center gap-1 px-2 py-1 text-xs text-muted-foreground font-medium min-w-0">
                        <FolderOpen className="h-3 w-3 shrink-0" />
                        <span className="truncate">{folder}</span>
                      </div>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" align="start" className="max-w-xs break-all">{folder}</TooltipContent>
                  </Tooltip>
                )}
                {folderFiles.map((file) => (
                  <button
                    key={file.id}
                    onClick={() => setSelectedFileId(file.id === selectedFileId ? null : file.id)}
                    className={`w-full flex items-center gap-2 px-2 py-2 rounded-md text-sm text-left transition-colors min-w-0 ${
                      file.indexing_excluded ? 'opacity-50' : ''
                    } ${
                      selectedFileId === file.id
                        ? 'bg-accent text-accent-foreground'
                        : 'hover:bg-accent/50'
                    }`}
                  >
                    {file.indexing_excluded ? (
                      <Ban className="h-4 w-4 text-muted-foreground shrink-0" />
                    ) : (
                      mimeIcon(file.mime_type)
                    )}
                    <span className="flex-1 truncate">{file.source_file_name}</span>
                    {file.sync_status === 'pending' && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Badge variant="secondary" className="text-xs shrink-0 gap-1 text-muted-foreground">
                            <Clock className="h-3 w-3" />
                            queued
                          </Badge>
                        </TooltipTrigger>
                        <TooltipContent>Waiting to be processed</TooltipContent>
                      </Tooltip>
                    )}
                    {file.sync_status === 'syncing' && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Badge variant="secondary" className="text-xs shrink-0 gap-1 bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
                            <Loader2 className="h-3 w-3 animate-spin" />
                            syncing
                          </Badge>
                        </TooltipTrigger>
                        <TooltipContent>Processing file...</TooltipContent>
                      </Tooltip>
                    )}
                    {file.sync_status === 'failed' && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Badge variant="destructive" className="text-xs shrink-0 gap-1">
                            <AlertCircle className="h-3 w-3" />
                            failed
                          </Badge>
                        </TooltipTrigger>
                        <TooltipContent>Sync failed for this file</TooltipContent>
                      </Tooltip>
                    )}
                    {file.sync_status === 'skipped' && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Badge variant="secondary" className="text-xs shrink-0 gap-1 bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                            <SkipForward className="h-3 w-3" />
                            skipped
                          </Badge>
                        </TooltipTrigger>
                        <TooltipContent>{(file as any).skip_reason || 'File exceeds processing limits'}</TooltipContent>
                      </Tooltip>
                    )}
                    {(!file.sync_status || file.sync_status === 'synced') && !file.indexing_excluded && file.page_count != null && file.page_count > 0 && (
                      <IndexBadge pageCount={file.page_count} indexedPages={file.indexed_pages ?? 0} isReindexing={isReindexing} />
                    )}
                    {file.indexing_excluded && (
                      <Badge variant="secondary" className="text-xs shrink-0 text-muted-foreground">
                        excluded
                      </Badge>
                    )}
                    <ChevronRight className={`h-3 w-3 shrink-0 transition-transform ${
                      selectedFileId === file.id ? 'rotate-90' : ''
                    }`} />
                  </button>
                ))}
              </div>
            ))}
          </div>
          )}
        </ScrollArea>
        {/* Pagination controls */}
        {totalPageCount > 1 && (
          <div className="flex items-center justify-between px-3 py-2 border-t bg-muted/50 text-xs text-muted-foreground">
            <span>{page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, totalFiles)} of {totalFiles.toLocaleString()}</span>
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                disabled={page === 0}
                onClick={() => { setPage(p => p - 1); setSelectedFileId(null); }}
              >
                <ChevronLeft className="h-4 w-4" />
              </Button>
              <span>{page + 1} / {totalPageCount}</span>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                disabled={page >= totalPageCount - 1}
                onClick={() => { setPage(p => p + 1); setSelectedFileId(null); }}
              >
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
        )}
      </div>

      {/* Page thumbnails / details */}
      <div className="lg:col-span-2 border rounded-lg">
        {selectedFileId && files.find(f => f.id === selectedFileId) ? (
          <FileDetail
            catalogId={catalogId}
            fileId={selectedFileId}
            file={files.find(f => f.id === selectedFileId)!}
            canEdit={canEdit}
          />
        ) : (
          <div className="flex items-center justify-center h-[calc(100vh-20rem)] text-muted-foreground">
            Select a file to view its pages
          </div>
        )}
      </div>
    </div>
  );
}

function FileDetail({ catalogId, fileId, file, canEdit }: {
  catalogId: string;
  fileId: string;
  file: CatalogFile;
  canEdit?: boolean;
}) {
  const PAGES_PER_PAGE = 9;
  const [summaryExpanded, setSummaryExpanded] = useState(false);
  const [showMetadata, setShowMetadata] = useState(false);
  const [slidePage, setSlidePage] = useState(0);
  const queryClient = useQueryClient();

  const { data: pages } = useQuery({
    ...listFilePagesOptions({
      path: { catalog_id: catalogId, file_id: fileId },
    }),
    enabled: !!fileId,
  });

  const toggleIndexingMutation = useMutation({
    ...updateFileIndexingMutation(),
    onSuccess: () => {
      toast.success(file.indexing_excluded ? 'File included in indexing' : 'File excluded from indexing');
      queryClient.invalidateQueries({ queryKey: listCatalogFilesQueryKey({ path: { catalog_id: catalogId } }) });
    },
    onError: (err) => {
      toast.error('Failed to update indexing', { description: getErrorMessage(err) });
    },
  });

  const pageList = (pages as CatalogPage[] | undefined) ?? [];
  const totalSlidePages = Math.ceil(pageList.length / PAGES_PER_PAGE);
  const paginatedPages = pageList.slice(slidePage * PAGES_PER_PAGE, (slidePage + 1) * PAGES_PER_PAGE);
  const summaryText = file.summary || '';
  const isLongSummary = summaryText.length > 150;

  // Reset slide page when file changes
  useEffect(() => { setSlidePage(0); }, [fileId]);

  return (
    <div className="flex flex-col h-[calc(100vh-20rem)]">
      {/* File header */}
      <div className="p-3 border-b bg-muted/50 space-y-2">
        <div className="flex items-center gap-2">
          {mimeIcon(file.mime_type)}
          <h3 className="text-sm font-medium truncate flex-1">{file.source_file_name}</h3>
          <a
            href={driveUrl(file.source_file_id)}
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0"
            onClick={(e) => e.stopPropagation()}
          >
            <Button variant="ghost" size="icon" className="h-7 w-7">
              <ExternalLink className="h-3.5 w-3.5" />
            </Button>
          </a>
        </div>

        {/* Summary with expand/collapse */}
        {summaryText && (
          <div>
            <p className={`text-xs text-muted-foreground ${!summaryExpanded && isLongSummary ? 'line-clamp-2' : ''}`}>
              {summaryText}
            </p>
            {isLongSummary && (
              <button
                onClick={() => setSummaryExpanded(!summaryExpanded)}
                className="text-xs text-primary hover:underline mt-0.5 flex items-center gap-0.5"
              >
                {summaryExpanded ? (
                  <>Show less</>
                ) : (
                  <>Show more</>
                )}
              </button>
            )}
          </div>
        )}

        {/* File metadata bar */}
        <div className="flex items-center gap-3 text-xs text-muted-foreground flex-wrap">
          {file.page_count != null && (
            <span className="flex items-center gap-1">
              <Hash className="h-3 w-3" />
              {file.page_count} pages
            </span>
          )}
          {file.mime_type && (
            <span className="truncate max-w-[180px]">{file.mime_type.split('/').pop()?.split('.').pop()}</span>
          )}
          {file.synced_at && (
            <span className="flex items-center gap-1">
              <Calendar className="h-3 w-3" />
              Synced {formatDate(file.synced_at as unknown as string)}
            </span>
          )}
          {file.source_modified_at && (
            <span className="flex items-center gap-1">
              Modified {formatDate(file.source_modified_at as unknown as string)}
            </span>
          )}
          {file.metadata && Object.keys(file.metadata).length > 0 && (
            <button
              onClick={() => setShowMetadata(!showMetadata)}
              className="flex items-center gap-0.5 text-primary hover:underline"
            >
              <Info className="h-3 w-3" />
              metadata
            </button>
          )}
        </div>

        {/* Expandable metadata */}
        {showMetadata && file.metadata && Object.keys(file.metadata).length > 0 && (
          <pre className="text-xs bg-muted p-2 rounded overflow-x-auto max-h-32">
            {JSON.stringify(file.metadata, null, 2)}
          </pre>
        )}

        {/* Indexing exclusion toggle */}
        {canEdit && (
          <div className="flex items-center justify-between pt-1">
            <label className="text-xs text-muted-foreground flex items-center gap-1.5 cursor-pointer">
              <Switch
                checked={!file.indexing_excluded}
                onCheckedChange={(checked) => {
                  toggleIndexingMutation.mutate({
                    path: { catalog_id: catalogId, file_id: fileId },
                    body: { indexing_excluded: !checked },
                  });
                }}
                disabled={toggleIndexingMutation.isPending}
                className="scale-75"
              />
              Include in vector search
            </label>
            {file.indexing_excluded && (
              <Badge variant="destructive" className="text-xs">
                <Ban className="h-3 w-3 mr-1" />
                Excluded from indexing
              </Badge>
            )}
          </div>
        )}
      </div>

      {/* Pages grid */}
      <ScrollArea className="flex-1 min-h-0">
        {file.indexing_excluded ? (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground text-sm gap-2 p-6">
            <Ban className="h-8 w-8 text-muted-foreground/30" />
            <p>This file is excluded from indexing</p>
            <p className="text-xs">Pages are still synced but not included in vector search</p>
          </div>
        ) : paginatedPages.length > 0 ? (
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 p-3">
            {paginatedPages.map((page) => (
              <PageThumbnail key={page.id} catalogId={catalogId} page={page} />
            ))}
          </div>
        ) : (
          <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
            No pages found for this file
          </div>
        )}
      </ScrollArea>

      {/* Page pagination */}
      {totalSlidePages > 1 && (
        <div className="flex items-center justify-center gap-2 p-2 border-t text-xs text-muted-foreground">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            disabled={slidePage <= 0}
            onClick={() => setSlidePage(p => p - 1)}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <span>{slidePage + 1} / {totalSlidePages}</span>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            disabled={slidePage >= totalSlidePages - 1}
            onClick={() => setSlidePage(p => p + 1)}
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      )}
    </div>
  );
}

function IndexBadge({ pageCount, indexedPages, isReindexing }: { pageCount: number; indexedPages: number; isReindexing?: boolean }) {
  const fullyIndexed = indexedPages >= pageCount;
  const partiallyIndexed = indexedPages > 0 && indexedPages < pageCount;

  if (fullyIndexed) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant="secondary" className="text-xs shrink-0 gap-1 bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400">
            <Check className="h-3 w-3" />
            {pageCount}p
          </Badge>
        </TooltipTrigger>
        <TooltipContent>All {pageCount} pages indexed</TooltipContent>
      </Tooltip>
    );
  }

  if (isReindexing && partiallyIndexed) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant="secondary" className="text-xs shrink-0 gap-1 bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
            <Loader2 className="h-3 w-3 animate-spin" />
            {indexedPages}/{pageCount}p
          </Badge>
        </TooltipTrigger>
        <TooltipContent>Re-indexing... {indexedPages} of {pageCount} pages indexed</TooltipContent>
      </Tooltip>
    );
  }

  if (partiallyIndexed) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant="secondary" className="text-xs shrink-0 gap-1 bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
            <Search className="h-3 w-3" />
            {indexedPages}/{pageCount}p
          </Badge>
        </TooltipTrigger>
        <TooltipContent>{indexedPages} of {pageCount} pages indexed</TooltipContent>
      </Tooltip>
    );
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="secondary" className="text-xs shrink-0 text-muted-foreground">
          {pageCount}p
        </Badge>
      </TooltipTrigger>
      <TooltipContent>Not yet indexed</TooltipContent>
    </Tooltip>
  );
}

function PageThumbnail({ catalogId, page }: { catalogId: string; page: CatalogPage }) {
  const [showDetails, setShowDetails] = useState(false);
  const thumbnailUrl = page.thumbnail_s3_key
    ? `/api/v1/catalogs/${catalogId}/pages/${page.id}/thumbnail`
    : null;

  return (
    <div className="rounded-md border overflow-hidden bg-muted/30">
      <div className="aspect-[4/3] relative bg-muted flex items-center justify-center">
        {thumbnailUrl ? (
          <img
            src={thumbnailUrl}
            alt={`Page ${page.page_number}`}
            className="w-full h-full object-contain"
            loading="lazy"
          />
        ) : (
          <FileIcon className="h-8 w-8 text-muted-foreground/30" />
        )}
      </div>
      <div className="p-2">
        <p className="text-xs font-medium truncate">
          {page.title || `Page ${page.page_number}`}
        </p>
        <div className="flex items-center justify-between">
          <p className="text-xs text-muted-foreground">Page {page.page_number}</p>
          <div className="flex items-center gap-1">
            <Tooltip>
              <TooltipTrigger asChild>
                <button onClick={() => setShowDetails(!showDetails)} className="p-0.5 hover:bg-accent rounded">
                  {showDetails ? <ChevronDown className="h-3 w-3 text-muted-foreground" /> : <Info className="h-3 w-3 text-muted-foreground" />}
                </button>
              </TooltipTrigger>
              <TooltipContent>{showDetails ? 'Hide details' : 'Show details'}</TooltipContent>
            </Tooltip>
            {page.indexed_at ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Check className="h-3 w-3 text-green-500" />
                </TooltipTrigger>
                <TooltipContent>Indexed {formatDate(page.indexed_at as unknown as string)}</TooltipContent>
              </Tooltip>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="h-2 w-2 rounded-full bg-muted-foreground/30 inline-block" />
                </TooltipTrigger>
                <TooltipContent>Not indexed</TooltipContent>
              </Tooltip>
            )}
          </div>
        </div>
        {/* Expandable page details */}
        {showDetails && (
          <div className="mt-2 pt-2 border-t space-y-1 text-xs text-muted-foreground">
            {page.content_hash && (
              <p className="truncate">Hash: {page.content_hash.slice(0, 16)}…</p>
            )}
            {page.source_ref && Object.keys(page.source_ref).length > 0 && (
              <p className="truncate">Source: {JSON.stringify(page.source_ref)}</p>
            )}
            {page.text_content && (
              <p className="line-clamp-3 text-foreground/70">{page.text_content}</p>
            )}
            {page.speaker_notes && (
              <p className="line-clamp-2 italic">Notes: {page.speaker_notes}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
