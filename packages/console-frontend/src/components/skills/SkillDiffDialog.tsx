import { useState, useEffect } from 'react';
import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued';
import { FileText, Loader2 } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { getSkillDetailApiV1SkillsRegistryDetailSkillIdGet, getSkillVersionDetailApiV1SkillsRegistryDetailSkillIdVersionsContentHashGet } from '@/api/generated/sdk.gen';

interface SkillFile {
  path: string;
  content: string;
}

interface FileDiff {
  path: string;
  status: 'modified' | 'added' | 'removed';
  oldContent: string;
  newContent: string;
}

interface SkillDiffDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  registryId: string;
  pinnedContentHash: string;
  skillName: string;
  onConfirmUpdate?: () => void | Promise<void>;
  confirmLabel?: string;
  confirmPending?: boolean;
}

export function SkillDiffDialog({
  open,
  onOpenChange,
  registryId,
  pinnedContentHash,
  skillName,
  onConfirmUpdate,
  confirmLabel,
  confirmPending,
}: SkillDiffDialogProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileDiffs, setFileDiffs] = useState<FileDiff[]>([]);

  useEffect(() => {
    if (!open || !registryId || !pinnedContentHash) return;

    let cancelled = false;
    setLoading(true);
    setError(null);
    setFileDiffs([]);

    (async () => {
      try {
        // Fetch pinned version (what we have)
        const pinnedRes = await getSkillVersionDetailApiV1SkillsRegistryDetailSkillIdVersionsContentHashGet({
          path: { skill_id: registryId, content_hash: pinnedContentHash },
          throwOnError: true,
        });
        const pinnedData = pinnedRes.data as any;
        const pinnedFiles: SkillFile[] = pinnedData?.files ?? [];

        // Fetch latest version (what's available)
        const latestRes = await getSkillDetailApiV1SkillsRegistryDetailSkillIdGet({
          path: { skill_id: registryId },
          throwOnError: true,
        });
        const latestData = latestRes.data as any;
        const latestFiles: SkillFile[] = latestData?.files ?? [];

        if (cancelled) return;

        // Compute file diffs
        const pinnedMap = new Map(pinnedFiles.map((f) => [f.path, f.content]));
        const latestMap = new Map(latestFiles.map((f) => [f.path, f.content]));
        const allPaths = new Set([...pinnedMap.keys(), ...latestMap.keys()]);

        const diffs: FileDiff[] = [];
        for (const path of allPaths) {
          const oldContent = pinnedMap.get(path) ?? '';
          const newContent = latestMap.get(path) ?? '';
          if (oldContent === newContent) continue;

          let status: FileDiff['status'] = 'modified';
          if (!pinnedMap.has(path)) status = 'added';
          else if (!latestMap.has(path)) status = 'removed';

          diffs.push({ path, status, oldContent, newContent });
        }

        // Sort: SKILL.md first, then alphabetical
        diffs.sort((a, b) => {
          if (a.path === 'SKILL.md') return -1;
          if (b.path === 'SKILL.md') return 1;
          return a.path.localeCompare(b.path);
        });

        setFileDiffs(diffs);
      } catch (e: any) {
        if (!cancelled) {
          setError(e?.message || 'Failed to load diff');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [open, registryId, pinnedContentHash]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[95vw] max-w-[95vw] sm:max-w-[95vw] h-[90vh] max-h-[90vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>Update Available — {skillName}</DialogTitle>
          <DialogDescription>
            Changes between your pinned version and the latest registry version.
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto border rounded-md min-h-0">
          {loading && (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              <span className="ml-2 text-sm text-muted-foreground">Loading diff…</span>
            </div>
          )}
          {error && (
            <div className="flex items-center justify-center py-12">
              <span className="text-sm text-destructive">{error}</span>
            </div>
          )}
          {!loading && !error && fileDiffs.length === 0 && (
            <div className="flex items-center justify-center py-12">
              <span className="text-sm text-muted-foreground">No differences found.</span>
            </div>
          )}
          {!loading && !error && fileDiffs.map((diff) => (
            <div key={diff.path} className="border-b last:border-b-0">
              <div className="px-3 py-1.5 bg-muted/50 border-b flex items-center gap-2 text-xs font-medium sticky top-0 z-10">
                <FileText className="h-3 w-3" />
                <span>{diff.path}</span>
                <Badge
                  variant={diff.status === 'added' ? 'default' : diff.status === 'removed' ? 'destructive' : 'secondary'}
                  className="text-[9px] px-1 py-0"
                >
                  {diff.status}
                </Badge>
              </div>
              <ReactDiffViewer
                oldValue={diff.oldContent}
                newValue={diff.newContent}
                splitView={false}
                compareMethod={DiffMethod.LINES}
                useDarkTheme={document.documentElement.classList.contains('dark')}
                hideLineNumbers={false}
                styles={{ contentText: { fontSize: '11px', lineHeight: '1.4' } }}
              />
            </div>
          ))}
        </div>
        <DialogFooter className="mt-3">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={!!confirmPending}>
            {onConfirmUpdate ? 'Cancel' : 'Close'}
          </Button>
          {onConfirmUpdate && (
            <Button
              onClick={async () => {
                await onConfirmUpdate();
              }}
              disabled={loading || !!error || confirmPending}
            >
              {confirmPending && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />}
              {confirmLabel ?? 'Update'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
