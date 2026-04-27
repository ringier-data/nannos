import { Progress } from '@/components/ui/progress';
import { Button } from '@/components/ui/button';
import { RefreshCw, CheckCircle2, XCircle, Pause, Play, Square, Ban, ChevronDown, ChevronUp } from 'lucide-react';
import type { CatalogSyncJob } from '@/api/generated/types.gen';
import { useState } from 'react';

interface SyncStatusBarProps {
  syncJob: CatalogSyncJob;
  onPause?: () => void;
  onResume?: () => void;
  onCancel?: () => void;
}

export function SyncStatusBar({ syncJob, onPause, onResume, onCancel }: SyncStatusBarProps) {
  const [showErrors, setShowErrors] = useState(false);
  // Page-level progress is ephemeral (Socket.IO only, not persisted in DB),
  // so these fields are not part of the typed CatalogSyncJob model.
  const raw = syncJob as Record<string, unknown>;
  const pageTotal = (raw.current_file_pages_total as number) ?? 0;
  // Clamp: concurrent file processing can leave stale pages_done > pages_total
  const pageDone = Math.min((raw.current_file_pages_done as number) ?? 0, Math.max(pageTotal, 1));  const currentFileName = (raw.current_file_name as string) ?? null;
  // Blend file + page progress for a smoother bar
  const blendedProgress = (syncJob.total_files ?? 0) > 0
    ? Math.round(
        (((syncJob.processed_files ?? 0) + (pageTotal > 0 ? Math.min(pageDone / pageTotal, 1) : 0)) /
          (syncJob.total_files ?? 0)) *
          100,
      )
    : 0;

  const isRunning = syncJob.status === 'running';
  const isPending = syncJob.status === 'pending';
  const isPaused = syncJob.status === 'paused';
  const isCancelling = syncJob.status === 'cancelling';
  const isCancelled = syncJob.status === 'cancelled';
  const isCompleted = syncJob.status === 'completed';
  const isFailed = syncJob.status === 'failed';
  const isReindexing = syncJob.status === 'reindexing';
  const isActive = isRunning || isPending || isPaused || isCancelling || isReindexing;

  const statusLabel = isPending ? 'Starting sync...'
    : isRunning ? 'Syncing...'
    : isReindexing ? 'Re-indexing...'
    : isPaused ? 'Paused'
    : isCancelling ? 'Cancelling...'
    : isCancelled ? 'Sync cancelled'
    : isCompleted && (syncJob.failed_files ?? 0) > 0 ? 'Sync completed with errors'
    : isCompleted ? 'Sync complete'
    : isFailed ? 'Sync failed'
    : syncJob.status;

  // Parse error_details: can be a single {error} object or an array of {file, error}
  const errorDetails: { file: string; error: string }[] = Array.isArray(syncJob.error_details)
    ? (syncJob.error_details as { file: string; error: string }[])
    : [];
  const singleError = !Array.isArray(syncJob.error_details) && syncJob.error_details
    ? (syncJob.error_details as { error?: string }).error ?? null
    : null;

  return (
    <div className="rounded-lg border p-4">
      <div className="flex items-center gap-3 mb-2">
        {(isRunning || isPending || isReindexing) && <RefreshCw className="h-4 w-4 animate-spin text-blue-500" />}
        {isPaused && <Pause className="h-4 w-4 text-amber-500" />}
        {isCancelling && <RefreshCw className="h-4 w-4 animate-spin text-muted-foreground" />}
        {isCancelled && <Ban className="h-4 w-4 text-muted-foreground" />}
        {isCompleted && (syncJob.failed_files ?? 0) === 0 && <CheckCircle2 className="h-4 w-4 text-green-500" />}
        {isCompleted && (syncJob.failed_files ?? 0) > 0 && <XCircle className="h-4 w-4 text-amber-500" />}
        {isFailed && <XCircle className="h-4 w-4 text-destructive" />}
        <span className="text-sm font-medium">{statusLabel}</span>
        <span className="text-xs text-muted-foreground ml-auto">
          {syncJob.processed_files}/{syncJob.total_files} {isReindexing ? 'pages' : 'files'}
          {(syncJob.failed_files ?? 0) > 0 && ` (${syncJob.failed_files} failed)`}
        </span>
        {/* Controls */}
        <div className="flex items-center gap-1">
          {isRunning && onPause && (
            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onPause} title="Pause sync">
              <Pause className="h-3.5 w-3.5" />
            </Button>
          )}
          {isPaused && onResume && (
            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onResume} title="Resume sync">
              <Play className="h-3.5 w-3.5" />
            </Button>
          )}
          {(isRunning || isPending || isPaused) && onCancel && (
            <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive hover:text-destructive" onClick={onCancel} title="Cancel sync">
              <Square className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>
      {isActive && (
        <>
          <Progress value={blendedProgress} className={`h-2 ${isPaused ? 'opacity-60' : ''}`} />
          <p className="text-xs text-muted-foreground mt-1 truncate">
            {currentFileName
              ? (syncJob.total_files ?? 0) === 0
                ? currentFileName
                : `${isPaused ? 'Paused at' : 'Processing'}: ${currentFileName}`
              : 'Preparing...'}
            {currentFileName && (syncJob.total_files ?? 0) > 0 && pageTotal > 0 && ` (page ${pageDone}/${pageTotal})`}
          </p>
        </>
      )}
      {/* Error details */}
      {errorDetails.length > 0 && (
        <div className="mt-2">
          <button
            onClick={() => setShowErrors(!showErrors)}
            className="flex items-center gap-1 text-xs text-destructive hover:underline"
          >
            {showErrors ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            {errorDetails.length} file{errorDetails.length !== 1 ? 's' : ''} failed
          </button>
          {showErrors && (
            <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground max-h-40 overflow-y-auto">
              {errorDetails.map((err, i) => (
                <li key={i} className="flex gap-1">
                  <XCircle className="h-3 w-3 text-destructive mt-0.5 shrink-0" />
                  <span><span className="font-medium">{err.file}</span>: {err.error}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {singleError && errorDetails.length === 0 && (
        <p className="mt-2 text-xs text-destructive">{singleError}</p>
      )}
    </div>
  );
}
