import { useState, useCallback, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued';
import {
  BookOpen,
  Search,
  Plus,
  Trash2,
  Loader2,
  Globe,
  Lock,
  FilePlus,
  FileText,
  Save,
  X,
  PencilLine,
  Copy,
  RefreshCw,
  Import,
  GitBranch,
  History,
  RotateCcw,
  Download,
  ChevronLeft,
  ExternalLink,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  searchSkillsApiV1SkillsRegistrySearchGetOptions,
  searchSkillsApiV1SkillsRegistrySearchGetQueryKey,
  removeSkillApiV1SkillsRegistrySkillIdDeleteMutation,
  getSkillDetailApiV1SkillsRegistryDetailSkillIdGetOptions,
  getSkillDetailApiV1SkillsRegistryDetailSkillIdGetQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import {
  writeRegistryFileApiV1SkillsRegistrySkillIdFilesFilePathPut,
  deleteRegistryFileApiV1SkillsRegistrySkillIdFilesFilePathDelete,
  createRegistrySkillApiV1SkillsRegistryPost,
  updateRegistrySkillApiV1SkillsRegistrySkillIdPut,
} from '@/api/generated/sdk.gen';
import type { SkillSearchResult } from '@/api/generated/types.gen';
import { client } from '@/api/generated/client.gen';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogDescription,
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { SkillImportPanel } from '@/components/skills/SkillImportPanel';

// --- Helpers for SKILL.md structured editing ---

function parseSkillMd(raw: string): { description: string; body: string } {
  const trimmed = raw.trim();
  if (!trimmed.startsWith('---')) return { description: '', body: trimmed };
  const endIdx = trimmed.indexOf('---', 3);
  if (endIdx === -1) return { description: '', body: trimmed };
  const frontmatter = trimmed.slice(3, endIdx);
  const body = trimmed.slice(endIdx + 3).replace(/^\n+/, '');
  let description = '';
  for (const line of frontmatter.split('\n')) {
    const match = line.match(/^description:\s*(.*)$/);
    if (match) description = match[1].trim();
  }
  return { description, body };
}

function composeSkillMd(name: string, description: string, body: string): string {
  const lines = ['---', `name: ${name}`];
  if (description) lines.push(`description: ${description}`);
  lines.push('---', '');
  if (body) lines.push(body);
  let content = lines.join('\n');
  if (!content.endsWith('\n')) content += '\n';
  return content;
}

// --- Main component ---

export function SkillRegistryPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const searchQuery = searchParams.get('q') || '';

  const [showImport, setShowImport] = useState(false);
  const [selectedSkillId, setSelectedSkillIdRaw] = useState<string | null>(() => {
    // Support deep-linking via ?skill=<slug> (or legacy ?skill=<id>)
    const skillParam = searchParams.get('skill');
    if (skillParam) return skillParam;
    try {
      const stored = sessionStorage.getItem('skill-registry-draft');
      if (stored) {
        const parsed = JSON.parse(stored);
        if (parsed?.existingId) return parsed.existingId;
      }
    } catch { /* ignore */ }
    return null;
  });

  // Wrapper that keeps the URL ?skill= param in sync with selection
  const setSelectedSkillId = useCallback((id: string | null) => {
    setSelectedSkillIdRaw(id);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (id) next.set('skill', id);
      else next.delete('skill');
      return next;
    }, { replace: false });
  }, [setSearchParams]);

  // Sync selection when browser back/forward changes the URL
  useEffect(() => {
    const urlSkill = searchParams.get('skill');
    setSelectedSkillIdRaw(urlSkill);
  }, [searchParams]);

  const [deletingSkill, setDeletingSkill] = useState<SkillSearchResult | null>(null);

  // Draft skill state — persisted to sessionStorage so refresh doesn't lose work
  const DRAFT_KEY = 'skill-registry-draft';
  const draftSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [draftSkill, setDraftSkillRaw] = useState<{
    name: string;
    slug: string;
    files: Array<{ path: string; content: string }>;
    existingId?: string;  // set when editing an existing skill
    originalFiles?: Array<{ path: string; content: string }>;  // server state for diffing
    sandboxRequired?: boolean;
    originalName?: string;  // server name for diffing
    visibility?: string;  // 'private' | 'public'
  } | null>(() => {
    try {
      const stored = sessionStorage.getItem(DRAFT_KEY);
      return stored ? JSON.parse(stored) : null;
    } catch { return null; }
  });
  const setDraftSkill = useCallback((draft: typeof draftSkill) => {
    setDraftSkillRaw(draft);
    if (draftSaveTimer.current) clearTimeout(draftSaveTimer.current);
    if (draft) {
      draftSaveTimer.current = setTimeout(() => {
        sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
      }, 500);
    } else {
      sessionStorage.removeItem(DRAFT_KEY);
    }
  }, []);
  const [editingDraftName, setEditingDraftName] = useState(false);
  const [renamingSkillId, setRenamingSkillId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');

  // File editor state
  const [activeFile, setActiveFile] = useState<string>('SKILL.md');
  const [editedContent, setEditedContent] = useState<string | null>(null);
  const [editedDescription, setEditedDescription] = useState<string | null>(null);
  const [editedBody, setEditedBody] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [addingFile, setAddingFile] = useState(false);
  const [newFilePath, setNewFilePath] = useState('');
  const [deletingFile, setDeletingFile] = useState<string | null>(null);
  const [renamingFile, setRenamingFile] = useState<string | null>(null);
  const [renameFileValue, setRenameFileValue] = useState('');
  // File the user wants to switch to, pending confirmation to discard unsaved edits
  const [pendingFile, setPendingFile] = useState<string | null>(null);

  // Update check state
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updateDiff, setUpdateDiff] = useState<{ files: Array<{ path: string; current: string | null; latest: string | null; status: string }>; latest_hash: string } | null>(null);
  const [applyingUpdate, setApplyingUpdate] = useState(false);

  // Copy state
  const [copying, setCopying] = useState(false);

  // Version history state
  const [showHistory, setShowHistory] = useState(false);
  const [versionHistory, setVersionHistory] = useState<Array<{ content_hash: string; description: string; created_by: string; created_at: string }>>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<{ content_hash: string; files: Array<{ path: string; content: string }>; description: string; created_at: string; prevFiles: Array<{ path: string; content: string }> | null } | null>(null);
  const [restoringVersion, setRestoringVersion] = useState(false);

  const updateSearch = useCallback(
    (q: string) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        if (q) next.set('q', q);
        else next.delete('q');
        return next;
      }, { replace: true });
    },
    [setSearchParams],
  );

  // Search registry with pagination
  const PAGE_SIZE = 50;
  const [searchOffset, setSearchOffset] = useState(0);
  const [accumulatedSkills, setAccumulatedSkills] = useState<SkillSearchResult[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [totalCount, setTotalCount] = useState(0);

  const { data: searchData, isLoading: searchLoading } = useQuery({
    ...searchSkillsApiV1SkillsRegistrySearchGetOptions({
      query: { q: searchQuery || '*', source: 'registry', limit: PAGE_SIZE, offset: searchOffset } as any,
    }),
  });

  // Accumulate results as pages load
  useEffect(() => {
    if (searchData) {
      const newData = searchData.data ?? [];
      if (searchOffset === 0) {
        setAccumulatedSkills(newData);
      } else {
        setAccumulatedSkills((prev) => [...prev, ...newData]);
      }
      setHasMore((searchData as any).has_more ?? false);
      setTotalCount((searchData as any).total ?? newData.length);
    }
  }, [searchData, searchOffset]);

  // Reset offset when search query changes
  // Don't clear accumulatedSkills here — the data effect handles replacement
  // when offset is 0. Clearing here races with cached data on navigation.
  useEffect(() => {
    setSearchOffset(0);
  }, [searchQuery]);

  const skills = accumulatedSkills;
  const loadMore = () => setSearchOffset((prev) => prev + PAGE_SIZE);

  // Fetch detail for selected skill (includes files inline)
  const { data: skillDetail, isLoading: detailLoading } = useQuery({
    ...getSkillDetailApiV1SkillsRegistryDetailSkillIdGetOptions({
      path: { skill_id: selectedSkillId! },
    }),
    enabled: !!selectedSkillId,
  });
  const detail = skillDetail as any;
  const files: Array<{ path: string; content: string }> = detail?.files ?? [];

  // Reset editor state when switching skills
  useEffect(() => {
    setActiveFile('SKILL.md');
    setEditedContent(null);
    setEditedDescription(null);
    setEditedBody(null);
  }, [selectedSkillId]);

  // Imported skill = read-only
  const isImported = detail?.source_type && detail.source_type !== 'nannos';

  const invalidateSearch = () => {
    setSearchOffset(0);
    setAccumulatedSkills([]);
    queryClient.invalidateQueries({
      queryKey: searchSkillsApiV1SkillsRegistrySearchGetQueryKey({
        query: { q: searchQuery || '*', source: 'registry', limit: PAGE_SIZE, offset: 0 } as any,
      }),
    });
  };

  const invalidateDetail = () => {
    if (selectedSkillId) {
      queryClient.invalidateQueries({
        queryKey: getSkillDetailApiV1SkillsRegistryDetailSkillIdGetQueryKey({
          path: { skill_id: selectedSkillId },
        }),
      });
    }
  };

  // Delete mutation
  const deleteMutation = useMutation({
    ...removeSkillApiV1SkillsRegistrySkillIdDeleteMutation(),
    onSuccess: () => {
      toast.success('Skill removed from registry');
      invalidateSearch();
      if (deletingSkill?.id === selectedSkillId || deletingSkill?.slug === selectedSkillId) setSelectedSkillId(null);
      setDeletingSkill(null);
    },
    onError: () => toast.error('Failed to delete skill'),
  });

  const handleStartCreate = () => {
    setDraftSkill({
      name: 'new-skill',
      slug: 'new-skill',
      files: [{ path: 'SKILL.md', content: '# New Skill\n\n' }],
      visibility: 'public',
    });
    setEditingDraftName(true);
    setSelectedSkillId(null);
    setActiveFile('SKILL.md');
    setEditedContent(null);
    setEditedDescription(null);
    setEditedBody(null);
  };

  // Commit an inline rename — enters draft/editing mode with the new name
  const commitRename = (skillId: string, newName: string, originalName: string) => {
    const trimmed = newName.trim();
    if (!trimmed || trimmed === originalName) { setRenamingSkillId(null); return; }
    // If already in draft for this skill, just update the name
    if (draftSkill?.existingId === skillId) {
      setDraftSkill({ ...draftSkill, name: trimmed, slug: trimmed.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') });
      setRenamingSkillId(null);
      return;
    }
    // Enter draft mode with the new name (ensureEditDraft needs detail loaded)
    if ((selectedSkillId === skillId || detail?.id === skillId) && detail && !isImported) {
      const detailFiles: Array<{ path: string; content: string }> = detail.files ?? [];
      const draft = {
        name: trimmed,
        slug: trimmed.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, ''),
        files: detailFiles.map((f: { path: string; content: string }) => ({ ...f })),
        existingId: detail.id,
        originalFiles: detailFiles.map((f: { path: string; content: string }) => ({ ...f })),
        sandboxRequired: detail.sandbox_required ?? false,
        originalName: detail.name,
        visibility: detail.visibility ?? 'public',
      };
      setDraftSkill(draft);
    }
    setRenamingSkillId(null);
  };

  // --- File operations ---

  const isDraft = !!draftSkill && (!selectedSkillId || draftSkill.existingId === selectedSkillId || draftSkill.existingId === detail?.id);
  const isEditing = !!selectedSkillId || isDraft;
  const effectiveFiles = isDraft ? draftSkill.files : files;

  // Lazily enter draft mode on first edit of an existing skill
  const ensureEditDraft = () => {
    if (isDraft) return draftSkill;
    if (!selectedSkillId || !detail || isImported) return null;
    const detailFiles: Array<{ path: string; content: string }> = detail.files ?? [];
    const draft = {
      name: detail.name,
      slug: detail.slug ?? detail.name,
      files: detailFiles.map((f: { path: string; content: string }) => ({ ...f })),
      existingId: detail.id,
      originalFiles: detailFiles.map((f: { path: string; content: string }) => ({ ...f })),
      sandboxRequired: detail.sandbox_required ?? false,
      originalName: detail.name,
      visibility: detail.visibility ?? 'public',
    };
    setDraftSkill(draft);
    return draft;
  };
  const currentFileContent = effectiveFiles.find((f) => f.path === activeFile)?.content ?? '';
  const isSkillMd = activeFile === 'SKILL.md';

  const parsedSkillMd = isSkillMd ? parseSkillMd(currentFileContent) : null;
  const displayDescription = editedDescription ?? parsedSkillMd?.description ?? '';
  const displayBody = editedBody ?? parsedSkillMd?.body ?? '';
  const displayContent = editedContent ?? currentFileContent;

  const isDirty = isDraft
    ? (() => {
        if (!draftSkill?.existingId || !draftSkill.originalFiles) return false;
        if (draftSkill.name !== draftSkill.originalName) return true;
        if (draftSkill.sandboxRequired !== (detail?.sandbox_required ?? false)) return true;
        if ((draftSkill.visibility ?? 'public') !== (detail?.visibility ?? 'public')) return true;
        return JSON.stringify(draftSkill.files) !== JSON.stringify(draftSkill.originalFiles);
      })()
    : isSkillMd
      ? editedDescription !== null || editedBody !== null
      : editedContent !== null;

  // Auto-clear draft for existing skills when all changes are reverted
  useEffect(() => {
    if (isDraft && draftSkill?.existingId && !isDirty) {
      setDraftSkill(null);
    }
  }, [isDirty, isDraft, draftSkill?.existingId]);

  // Flush current edits into draft files before switching or saving
  const flushDraftEdits = () => {
    if (!draftSkill) return draftSkill;
    let updatedFiles = [...draftSkill.files];
    if (activeFile === 'SKILL.md' && (editedDescription !== null || editedBody !== null)) {
      const content = composeSkillMd(draftSkill.name, displayDescription, displayBody);
      updatedFiles = updatedFiles.map((f) => f.path === 'SKILL.md' ? { ...f, content: content } : f);
    } else if (activeFile !== 'SKILL.md' && editedContent !== null) {
      updatedFiles = updatedFiles.map((f) => f.path === activeFile ? { ...f, content: editedContent } : f);
    }
    return { ...draftSkill, files: updatedFiles };
  };

  const handleSaveDraft = async () => {
    if (!draftSkill) return;
    const flushed = flushDraftEdits();
    if (!flushed) return;
    setSaving(true);
    try {
      if (flushed.existingId) {
        // Update existing skill via bulk update
        const filesChanged = JSON.stringify(flushed.files) !== JSON.stringify(flushed.originalFiles);
        await updateRegistrySkillApiV1SkillsRegistrySkillIdPut({
          path: { skill_id: flushed.existingId },
          body: {
            name: flushed.name !== flushed.originalName ? flushed.name : undefined,
            files: filesChanged ? flushed.files : undefined,
            sandbox_required: flushed.sandboxRequired !== (detail?.sandbox_required ?? false) ? flushed.sandboxRequired : undefined,
            visibility: (flushed.visibility ?? 'public') !== (detail?.visibility ?? 'public') ? flushed.visibility : undefined,
          } as any,
          throwOnError: true,
        });
        toast.success('Skill saved');
        setDraftSkill(null);
        setEditedContent(null);
        setEditedDescription(null);
        setEditedBody(null);
        invalidateDetail();
        invalidateSearch();
      } else {
        // Create new skill
        const res = await createRegistrySkillApiV1SkillsRegistryPost({
          body: {
            name: flushed.name,
            slug: flushed.slug,
            files: flushed.files,
            visibility: flushed.visibility ?? 'public',
          } as any,
          throwOnError: true,
        });
        toast.success('Skill created');
        const newSlug = (res as any)?.data?.slug ?? (res as any)?.slug;
        const newId = newSlug ?? (res as any)?.data?.id ?? (res as any)?.id;
        setDraftSkill(null);
        setEditedContent(null);
        setEditedDescription(null);
        setEditedBody(null);
        invalidateSearch();
        if (newId) setSelectedSkillId(newId);
      }
    } catch {
      toast.error('Failed to save skill');
    } finally {
      setSaving(false);
    }
  };

  const handleAddFile = async () => {
    const path = newFilePath.trim();
    if (!path) return;

    // Enter draft mode for existing skills if not already
    const d = draftSkill ?? ensureEditDraft();

    if (d) {
      // Add file locally to draft
      if (d.files.some((f) => f.path === path)) {
        toast.error(`File "${path}" already exists`);
        return;
      }
      setDraftSkill({
        ...d,
        files: [...d.files, { path, content: '' }],
      });
      setAddingFile(false);
      setNewFilePath('');
      setActiveFile(path);
      return;
    }

    if (!detail?.id) return;
    setSaving(true);
    try {
      await writeRegistryFileApiV1SkillsRegistrySkillIdFilesFilePathPut({
        path: { skill_id: detail.id, file_path: path },
        body: { content: '' },
        throwOnError: true,
      });
      toast.success(`File "${path}" created`);
      setAddingFile(false);
      setNewFilePath('');
      invalidateDetail();
      setActiveFile(path);
    } catch {
      toast.error('Failed to create file');
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteFile = async () => {
    if (!deletingFile) return;

    // Enter draft mode for existing skills if not already
    const d = draftSkill ?? ensureEditDraft();

    if (d) {
      setDraftSkill({
        ...d,
        files: d.files.filter((f) => f.path !== deletingFile),
      });
      if (activeFile === deletingFile) setActiveFile('SKILL.md');
      setDeletingFile(null);
      return;
    }

    if (!detail?.id) return;
    setSaving(true);
    try {
      await deleteRegistryFileApiV1SkillsRegistrySkillIdFilesFilePathDelete({
        path: { skill_id: detail.id, file_path: deletingFile },
        throwOnError: true,
      });
      toast.success(`File "${deletingFile}" deleted`);
      if (activeFile === deletingFile) setActiveFile('SKILL.md');
      setDeletingFile(null);
      invalidateDetail();
    } catch {
      toast.error('Failed to delete file');
    } finally {
      setSaving(false);
    }
  };

  const handleRenameFile = async () => {
    const oldPath = renamingFile;
    const newPath = renameFileValue.trim();
    if (!oldPath || !newPath || oldPath === newPath) {
      setRenamingFile(null);
      return;
    }

    // Enter draft mode for existing skills if not already
    const d = draftSkill ?? ensureEditDraft();

    if (d) {
      setDraftSkill({
        ...d,
        files: d.files.map((f) => f.path === oldPath ? { ...f, path: newPath } : f),
      });
      setRenamingFile(null);
      if (activeFile === oldPath) setActiveFile(newPath);
      return;
    }

    if (!detail?.id) { setRenamingFile(null); return; }
    setSaving(true);
    try {
      const oldContent = files.find((f) => f.path === oldPath)?.content ?? '';
      await writeRegistryFileApiV1SkillsRegistrySkillIdFilesFilePathPut({
        path: { skill_id: detail.id, file_path: newPath },
        body: { content: oldContent },
        throwOnError: true,
      });
      await deleteRegistryFileApiV1SkillsRegistrySkillIdFilesFilePathDelete({
        path: { skill_id: detail.id, file_path: oldPath },
        throwOnError: true,
      });
      toast.success(`Renamed "${oldPath}" → "${newPath}"`);
      setRenamingFile(null);
      if (activeFile === oldPath) setActiveFile(newPath);
      invalidateDetail();
    } catch {
      toast.error('Failed to rename file');
    } finally {
      setSaving(false);
    }
  };

  const visibilityIcon = (v?: string) => {
    switch (v) {
      case 'private': return <Lock className="h-3 w-3" />;
      default: return <Globe className="h-3 w-3" />;
    }
  };

  const handleCopySkill = async () => {
    if (!detail?.id) return;
    setCopying(true);
    try {
      const { data, error } = await client.post({
        url: `/api/v1/skills/registry/${detail.id}/copy`,
        body: {},
        headers: { 'Content-Type': 'application/json' },
      });
      if (error) throw error;
      toast.success('Editable copy created');
      invalidateSearch();
      const newSlug = (data as any)?.slug;
      if (newSlug) setSelectedSkillId(newSlug);
      else if ((data as any)?.id) setSelectedSkillId((data as any).id);
    } catch {
      toast.error('Failed to copy skill');
    } finally {
      setCopying(false);
    }
  };

  const handleCheckUpdate = async () => {
    if (!detail?.id) return;
    setCheckingUpdate(true);
    try {
      const { data, error } = await client.post({
        url: `/api/v1/skills/registry/${detail.id}/check-update`,
        headers: { 'Content-Type': 'application/json' },
      });
      if (error) throw error;
      const result = data as any;
      if (!result.update_available) {
        toast.success('Already up to date');
      } else {
        setUpdateDiff({ files: result.files, latest_hash: result.latest_hash });
      }
    } catch {
      toast.error('Failed to check for updates');
    } finally {
      setCheckingUpdate(false);
    }
  };

  const handleApplyUpdate = async () => {
    if (!detail?.id || !updateDiff) return;
    setApplyingUpdate(true);
    try {
      const { error } = await client.post({
        url: `/api/v1/skills/registry/${detail.id}/apply-update`,
        body: { latest_hash: updateDiff.latest_hash },
        headers: { 'Content-Type': 'application/json' },
      });
      if (error) throw error;
      toast.success('Skill updated from source');
      setUpdateDiff(null);
      invalidateDetail();
      invalidateSearch();
    } catch {
      toast.error('Failed to apply update');
    } finally {
      setApplyingUpdate(false);
    }
  };

  const handleShowHistory = async () => {
    if (!detail?.id) return;
    setShowHistory(true);
    setLoadingHistory(true);
    setSelectedVersion(null);
    try {
      const { data, error } = await client.get({
        url: `/api/v1/skills/registry/detail/${detail.id}/versions`,
      });
      if (error) throw error;
      setVersionHistory((data as any)?.versions ?? []);
    } catch {
      toast.error('Failed to load version history');
      setShowHistory(false);
    } finally {
      setLoadingHistory(false);
    }
  };

  const handleViewVersion = async (contentHash: string) => {
    if (!detail?.id) return;
    try {
      const { data, error } = await client.get({
        url: `/api/v1/skills/registry/detail/${detail.id}/versions/${contentHash}`,
      });
      if (error) throw error;
      const v = data as any;

      // Find the previous version in history to show what this version changed
      const idx = versionHistory.findIndex((h) => h.content_hash === contentHash);
      let prevFiles: Array<{ path: string; content: string }> | null = null;
      if (idx >= 0 && idx < versionHistory.length - 1) {
        const prevHash = versionHistory[idx + 1].content_hash;
        try {
          const { data: prevData } = await client.get({
            url: `/api/v1/skills/registry/detail/${detail.id}/versions/${prevHash}`,
          });
          if (prevData) {
            prevFiles = (prevData as any)?.files ?? [];
          }
        } catch {
          // If we can't fetch the previous version, show files without diff
        }
      }

      setSelectedVersion({
        content_hash: contentHash,
        files: v?.files ?? [],
        description: v?.description ?? '',
        created_at: v?.created_at ?? '',
        prevFiles,
      });
    } catch {
      toast.error('Failed to load version');
    }
  };

  const handleRestoreVersion = async () => {
    if (!detail?.id || !selectedVersion) return;
    setRestoringVersion(true);
    try {
      await updateRegistrySkillApiV1SkillsRegistrySkillIdPut({
        path: { skill_id: detail.id },
        body: { files: selectedVersion.files } as any,
        throwOnError: true,
      });
      toast.success('Version restored');
      setShowHistory(false);
      setSelectedVersion(null);
      invalidateDetail();
      invalidateSearch();
    } catch {
      toast.error('Failed to restore version');
    } finally {
      setRestoringVersion(false);
    }
  };

  // --- Import mode ---
  if (showImport) {
    return (
      <SkillImportPanel
        onClose={() => setShowImport(false)}
        onImported={() => {
          setShowImport(false);
          invalidateSearch();
        }}
      />
    );
  }

  return (
    <div className="flex h-full">
      {/* Left panel: skill list (full-width when browsing, narrow sidebar when editing) */}
      <div className={`${isEditing ? 'w-72 border-r shrink-0' : 'flex-1'} flex flex-col transition-[width,flex] duration-300 ease-in-out overflow-hidden`}>
        {/* Header */}
        <div className={`flex items-center justify-between border-b transition-all duration-300 ${isEditing ? 'px-4 py-3' : 'px-6 py-4'}`}>
          <div className="flex items-center gap-2">
            <BookOpen className={`text-muted-foreground transition-all duration-300 ${isEditing ? 'h-4 w-4' : 'h-5 w-5'}`} />
            <h1 className={`font-semibold transition-all duration-300 ${isEditing ? 'text-sm' : 'text-lg'}`}>Registry</h1>
            {!isEditing && totalCount > 0 && (
              <span className="text-sm text-muted-foreground">{totalCount} skills</span>
            )}
          </div>
          <div className="flex items-center gap-1">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={handleStartCreate} disabled={!!draftSkill}>
                  <Plus className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Create skill</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={() => setShowImport(true)}>
                  <Import className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Import from external</TooltipContent>
            </Tooltip>
          </div>
        </div>

        {/* Search */}
        <div className={`border-b transition-all duration-300 ${isEditing ? 'px-3 py-2' : 'px-6 py-3'}`}>
          <div className={`relative ${isEditing ? '' : 'max-w-lg'}`}>
            <Search className={`absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground transition-all duration-300 ${isEditing ? 'h-3.5 w-3.5' : 'h-4 w-4'}`} />
            <Input
              placeholder="Search skills..."
              className={`pl-8 transition-all duration-300 ${isEditing ? 'h-8 text-xs' : 'h-9 text-sm'}`}
              value={searchQuery}
              onChange={(e) => updateSearch(e.target.value)}
            />
          </div>
        </div>

        {/* Skill list / browse grid */}
        <div className="flex-1 overflow-y-auto">
          {/* New draft entry (only in sidebar mode) */}
          {isEditing && draftSkill && !draftSkill.existingId && (
            <div className="py-1 border-b border-blue-200 dark:border-blue-800">
              <div
                onClick={() => { if (!editingDraftName) { setSelectedSkillId(null); setActiveFile('SKILL.md'); } }}
                className={`group w-full text-left px-3 py-2 flex items-center gap-2 hover:bg-accent/50 transition-colors cursor-pointer ${
                  !selectedSkillId ? 'bg-accent' : ''
                }`}
              >
                <FileText className="h-3.5 w-3.5 text-blue-600 shrink-0" />
                <div className="flex-1 min-w-0">
                  {editingDraftName ? (
                    <input
                      className="text-xs font-medium bg-transparent border-b border-blue-500 outline-none w-full"
                      value={draftSkill.name}
                      onChange={(e) => {
                        setDraftSkill({ ...draftSkill, name: e.target.value, slug: e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') });
                      }}
                      onBlur={() => { setEditingDraftName(false); }}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === 'Escape') setEditingDraftName(false); }}
                      autoFocus
                    />
                  ) : (
                    <p className="text-xs font-medium truncate">{draftSkill.name}</p>
                  )}
                  <p className="text-[10px] flex items-center gap-1 mt-0.5">
                    {(draftSkill.visibility ?? 'public') === 'private' ? (
                      <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400"><Lock className="h-2.5 w-2.5" />private</span>
                    ) : (
                      <span className="flex items-center gap-1 text-muted-foreground"><Globe className="h-2.5 w-2.5" />public</span>
                    )}
                    <span className="text-blue-600 dark:text-blue-400">· new</span>
                  </p>
                </div>
                {!editingDraftName && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span
                        className="p-0.5 opacity-0 group-hover:opacity-100 hover:text-blue-600 text-muted-foreground shrink-0"
                        onClick={(e) => {
                          e.stopPropagation();
                          setSelectedSkillId(null);
                          setEditingDraftName(true);
                        }}
                      >
                        <PencilLine className="h-3 w-3" />
                      </span>
                    </TooltipTrigger>
                    <TooltipContent>Rename</TooltipContent>
                  </Tooltip>
                )}
              </div>
            </div>
          )}
          {searchLoading ? (
            <div className={`space-y-2 ${isEditing ? 'p-3' : 'p-6'}`}>
              {[1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className={isEditing ? 'h-10 w-full rounded' : 'h-20 w-full rounded-lg'} />)}
            </div>
          ) : skills.length === 0 ? (
            <div className="text-center text-muted-foreground py-10 px-4">
              <BookOpen className="h-8 w-8 mx-auto mb-2 opacity-30" />
              <p className="text-xs">No skills found</p>
            </div>
          ) : (() => {
            const editableSkills = skills.filter((s) => !s.source_type || s.source_type === 'nannos');
            const importedSkills = skills.filter((s) => s.source_type && s.source_type !== 'nannos');

            const renderSkillItem = (skill: typeof skills[0]) => {
                const isSkillImported = !!skill.source_type && skill.source_type !== 'nannos';
                const isRenaming = renamingSkillId === skill.id;
                const displaySource = isSkillImported ? skill.source : skill.author;
                return (
                <div
                  key={skill.id}
                  onClick={() => {
                    if (isRenaming) return;
                    if (draftSkill?.existingId && draftSkill.existingId !== skill.id) {
                      setDraftSkillRaw(null);
                    }
                    setSelectedSkillId(skill.slug || skill.id);
                    setEditedContent(null);
                    setEditedDescription(null);
                    setEditedBody(null);
                    setActiveFile('SKILL.md');
                  }}
                  className={`group w-full text-left flex items-start gap-2 hover:bg-accent/50 transition-colors cursor-pointer ${
                    isEditing
                      ? `px-3 py-2 ${(selectedSkillId === skill.slug || selectedSkillId === skill.id) ? 'bg-accent' : ''}`
                      : `px-6 py-3 border-b ${(selectedSkillId === skill.slug || selectedSkillId === skill.id) ? 'bg-accent' : ''}`
                  }`}
                >
                  <FileText className={`shrink-0 mt-0.5 ${isEditing ? 'h-3.5 w-3.5' : 'h-4 w-4'} ${(draftSkill?.existingId === skill.id || selectedSkillId === skill.slug) ? 'text-blue-600' : 'text-muted-foreground'}`} />
                  <div className="flex-1 min-w-0">
                    {isRenaming ? (
                      <input
                        className="text-xs font-medium bg-transparent border-b border-primary outline-none w-full"
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onBlur={() => commitRename(skill.id, renameValue, skill.name)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') commitRename(skill.id, renameValue, skill.name);
                          if (e.key === 'Escape') setRenamingSkillId(null);
                        }}
                        autoFocus
                      />
                    ) : (
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span className={`font-medium truncate ${isEditing ? 'text-xs' : 'text-sm'}`}>{draftSkill?.existingId === skill.id ? draftSkill.name : skill.name}</span>
                        {displaySource && (
                          <>
                            <span className="text-muted-foreground/40 text-xs shrink-0">·</span>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <span className={`text-muted-foreground truncate ${isEditing ? 'text-[10px]' : 'text-xs'}`}>{displaySource}</span>
                              </TooltipTrigger>
                              <TooltipContent>{displaySource}</TooltipContent>
                            </Tooltip>
                          </>
                        )}
                      </div>
                    )}
                    {/* Description - only shown in browse mode */}
                    {!isEditing && !isRenaming && skill.description && (
                      <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{skill.description}</p>
                    )}
                    {(() => {
                      const vis = draftSkill?.existingId === skill.id ? (draftSkill.visibility ?? skill.visibility) : skill.visibility;
                      const isItemEditing = draftSkill?.existingId === skill.id;
                      return vis ? (
                        <p className={`flex items-center gap-1 ${isEditing ? 'text-[10px] mt-0.5' : 'text-xs mt-1.5'}`}>
                          {vis === 'private' ? (
                            <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400"><Lock className="h-2.5 w-2.5" />private</span>
                          ) : (
                            <span className="flex items-center gap-1 text-muted-foreground"><Globe className="h-2.5 w-2.5" />public</span>
                          )}
                          {isItemEditing && <span className="text-blue-600 dark:text-blue-400">· editing</span>}
                          {isSkillImported && <span className="text-muted-foreground">· imported</span>}
                        </p>
                      ) : null;
                    })()}
                  </div>
                  {/* Install count - only in browse mode */}
                  {!isEditing && (skill.installs ?? 0) > 0 && (
                    <span className="flex items-center gap-1 text-xs text-muted-foreground shrink-0 mt-0.5">
                      <Download className="h-3 w-3" />
                      {skill.installs}
                    </span>
                  )}
                  {isEditing && !isSkillImported && !isRenaming && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span
                          className="p-0.5 opacity-0 group-hover:opacity-100 hover:text-primary text-muted-foreground shrink-0"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (selectedSkillId !== skill.slug && selectedSkillId !== skill.id) {
                              if (draftSkill?.existingId && draftSkill.existingId !== skill.id) setDraftSkillRaw(null);
                              setSelectedSkillId(skill.slug || skill.id);
                              setActiveFile('SKILL.md');
                            }
                            setRenamingSkillId(skill.id);
                            setRenameValue(skill.name);
                          }}
                        >
                          <PencilLine className="h-3 w-3" />
                        </span>
                      </TooltipTrigger>
                      <TooltipContent>Rename</TooltipContent>
                    </Tooltip>
                  )}
                </div>
                );
            };

            return (
              <div className={isEditing ? 'py-1' : 'py-2'}>
                {editableSkills.length > 0 && (
                  <>
                    <div className={`font-semibold uppercase text-muted-foreground tracking-wider ${
                      isEditing ? 'px-3 py-1.5 text-[10px]' : 'px-6 py-2 text-xs'
                    }`}>
                      Editable
                    </div>
                    {editableSkills.map((skill) => renderSkillItem(skill))}
                  </>
                )}
                {importedSkills.length > 0 && (
                  <>
                    <div className={`font-semibold uppercase text-muted-foreground tracking-wider ${
                      isEditing
                        ? `px-3 py-1.5 text-[10px] ${editableSkills.length > 0 ? 'mt-2 border-t pt-2' : ''}`
                        : `px-6 py-2 text-xs ${editableSkills.length > 0 ? 'mt-4 border-t pt-4' : ''}`
                    }`}>
                      Imported
                    </div>
                    {importedSkills.map((skill) => renderSkillItem(skill))}
                  </>
                )}
                {hasMore && (
                  <div className={isEditing ? 'px-3 py-2' : 'px-6 py-4 text-center'}>
                    <button
                      onClick={loadMore}
                      disabled={searchLoading}
                      className={`text-muted-foreground hover:text-foreground hover:bg-accent rounded transition-colors disabled:opacity-50 ${
                        isEditing ? 'w-full px-3 py-2 text-xs' : 'px-4 py-2 text-sm border rounded-md'
                      }`}
                    >
                      {searchLoading ? 'Loading...' : `Load more (${accumulatedSkills.length} of ${totalCount})`}
                    </button>
                  </div>
                )}
              </div>
            );
          })()}
        </div>

        {/* Bottom actions (sidebar mode only) */}
        {isEditing && (
          <div className="border-t p-3">
            <p className="text-[10px] text-muted-foreground text-center">
              Use + to create or import to add skills
            </p>
          </div>
        )}
      </div>

      {/* Right panel: editor */}
      <div className={`flex flex-col transition-[width,opacity,flex] duration-300 ease-in-out ${isEditing ? 'flex-1 min-w-0 opacity-100' : 'w-0 min-w-0 opacity-0 overflow-hidden'}`}>
        {isEditing && (!isDraft && detailLoading ? (
          <div className="flex-1 flex items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <>
            {/* Editor toolbar */}
            <div className="flex items-center justify-between px-4 py-2 border-b bg-muted/30">
              <div className="flex items-center gap-2">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      className="p-0.5 hover:text-primary text-muted-foreground -ml-1"
                      onClick={() => {
                        if (isDraft && !draftSkill.existingId) return; // don't leave unsaved new draft
                        setSelectedSkillId(null);
                      }}
                    >
                      <ChevronLeft className="h-4 w-4" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>Back to browse</TooltipContent>
                </Tooltip>
                <span className="flex items-center gap-1">
                  <span className="font-medium text-sm">
                    {isDraft ? draftSkill.name : detail?.name}
                  </span>
                  {!isImported && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          className="p-0.5 hover:text-primary text-muted-foreground"
                          onClick={() => {
                            if (isDraft) {
                              setEditingDraftName(true);
                            } else {
                              const d = ensureEditDraft();
                              if (d) setEditingDraftName(true);
                            }
                          }}
                        >
                          <PencilLine className="h-3 w-3" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>Rename</TooltipContent>
                    </Tooltip>
                  )}
                </span>
                {isDraft && !draftSkill.existingId && (
                  <Badge variant="outline" className="text-[10px] px-1.5 py-0 gap-1 border-blue-500 text-blue-600">
                    new
                  </Badge>
                )}
                {isDraft && (
                  <Badge variant="outline" className="text-[10px] px-1.5 py-0 gap-1 border-blue-500 text-blue-600">
                    editing
                  </Badge>
                )}
                {isDraft ? (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <button className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 border rounded-md hover:bg-accent transition-colors">
                        {visibilityIcon(draftSkill.visibility ?? 'public')}
                        {draftSkill.visibility ?? 'public'}
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start">
                      <DropdownMenuItem onClick={() => setDraftSkill({ ...draftSkill, visibility: 'public' })} className="text-xs gap-2">
                        {visibilityIcon('public')} public
                      </DropdownMenuItem>
                      <DropdownMenuItem onClick={() => setDraftSkill({ ...draftSkill, visibility: 'private' })} className="text-xs gap-2">
                        {visibilityIcon('private')} private
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                ) : detail?.visibility ? (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <button className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 border rounded-md hover:bg-accent transition-colors">
                        {visibilityIcon(detail.visibility)}
                        {detail.visibility}
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start">
                      <DropdownMenuItem onClick={async () => {
                        if (isImported && detail?.id) {
                          try {
                            await updateRegistrySkillApiV1SkillsRegistrySkillIdPut({ path: { skill_id: detail.id }, body: { visibility: 'public' } as any, throwOnError: true });
                            invalidateDetail();
                            invalidateSearch();
                          } catch { toast.error('Failed to update visibility'); }
                        } else { const d = ensureEditDraft(); if (d) setDraftSkill({ ...d, visibility: 'public' }); }
                      }} className="text-xs gap-2">
                        {visibilityIcon('public')} public
                      </DropdownMenuItem>
                      <DropdownMenuItem onClick={async () => {
                        if (isImported && detail?.id) {
                          try {
                            await updateRegistrySkillApiV1SkillsRegistrySkillIdPut({ path: { skill_id: detail.id }, body: { visibility: 'private' } as any, throwOnError: true });
                            invalidateDetail();
                            invalidateSearch();
                          } catch { toast.error('Failed to update visibility'); }
                        } else { const d = ensureEditDraft(); if (d) setDraftSkill({ ...d, visibility: 'private' }); }
                      }} className="text-xs gap-2">
                        {visibilityIcon('private')} private
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                ) : null}
                {isImported && (
                  <Badge variant="secondary" className="text-[10px] px-1.5 py-0 gap-1">
                    <GitBranch className="h-2.5 w-2.5" />
                    imported
                  </Badge>
                )}
              </div>
              <div className="flex items-center gap-3">
                {(selectedSkillId || isDraft) && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                        <Switch
                          checked={isDraft ? (draftSkill.sandboxRequired ?? false) : (detail?.sandbox_required ?? false)}
                          onCheckedChange={async (checked) => {
                            if (isImported && detail?.id) {
                              // Imported skills: update sandbox_required directly via API
                              try {
                                await updateRegistrySkillApiV1SkillsRegistrySkillIdPut({
                                  path: { skill_id: detail.id },
                                  body: { sandbox_required: checked } as any,
                                  throwOnError: true,
                                });
                                invalidateDetail();
                                toast.success(checked ? 'Sandbox enabled' : 'Sandbox disabled');
                              } catch {
                                toast.error('Failed to update sandbox setting');
                              }
                            } else {
                              const d = draftSkill ?? ensureEditDraft();
                              if (d) setDraftSkill({ ...d, sandboxRequired: checked });
                            }
                          }}
                          className="scale-75"
                        />
                        Sandbox
                      </label>
                    </TooltipTrigger>
                    <TooltipContent>Run skill files in a sandboxed environment</TooltipContent>
                  </Tooltip>
                )}
                <div className="flex items-center gap-1.5">
                {isDraft ? (
                  <>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => {
                        setDraftSkill(null);
                        setEditedContent(null);
                        setEditedDescription(null);
                        setEditedBody(null);
                      }}
                    >
                      <X className="h-3 w-3 mr-1" />
                      Discard
                    </Button>
                    {(isDirty || !draftSkill.existingId) && (
                      <Button size="sm" className="h-7 text-xs" onClick={handleSaveDraft} disabled={saving}>
                        {saving ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Save className="h-3 w-3 mr-1" />}
                        Save
                      </Button>
                    )}
                  </>
                ) : isImported ? (
                  <>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-7 text-xs"
                          onClick={handleCheckUpdate}
                          disabled={checkingUpdate}
                        >
                          {checkingUpdate ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <RefreshCw className="h-3 w-3 mr-1" />}
                          Check Updates
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Check for upstream updates</TooltipContent>
                    </Tooltip>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-7 text-xs"
                          onClick={handleCopySkill}
                          disabled={copying}
                        >
                          {copying ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Copy className="h-3 w-3 mr-1" />}
                          Copy to Edit
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Create an editable copy</TooltipContent>
                    </Tooltip>
                  </>
                ) : null}
                {selectedSkillId && !isDraft && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 w-7 p-0"
                        onClick={handleShowHistory}
                      >
                        <History className="h-3.5 w-3.5" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Version history</TooltipContent>
                  </Tooltip>
                )}
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                      onClick={() => {
                        const skill = skills.find((s) => s.id === selectedSkillId || s.slug === selectedSkillId);
                        if (skill) setDeletingSkill(skill);
                      }}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Delete skill</TooltipContent>
                </Tooltip>
                </div>
              </div>
            </div>

            <div className="flex-1 flex min-h-0">
              {/* File tree sidebar */}
              <div className="w-48 border-r flex flex-col shrink-0">
                <div className="flex items-center justify-between px-3 py-2 border-b">
                  <span className="text-[10px] font-semibold uppercase text-muted-foreground tracking-wider">Files</span>
                  {!isImported && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-5 w-5 p-0"
                          onClick={() => { setAddingFile(true); setNewFilePath(''); }}
                        >
                          <FilePlus className="h-3 w-3" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Add file</TooltipContent>
                    </Tooltip>
                  )}
                </div>
                <div className="flex-1 overflow-y-auto py-1">
                  {effectiveFiles.map((f) => (
                    <div
                      key={f.path}
                      className={`group flex items-center gap-1.5 px-3 py-1 cursor-pointer hover:bg-accent/50 text-xs ${
                        activeFile === f.path ? 'bg-accent font-medium' : ''
                      }`}
                    >
                      {renamingFile === f.path ? (
                        <input
                          className="flex-1 bg-transparent border-b border-primary text-xs outline-none"
                          value={renameFileValue}
                          onChange={(e) => setRenameFileValue(e.target.value)}
                          onBlur={handleRenameFile}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') handleRenameFile();
                            if (e.key === 'Escape') setRenamingFile(null);
                          }}
                          autoFocus
                        />
                      ) : (
                        <>
                          <span
                            className="flex-1 truncate"
                            onClick={() => {
                              if (isDraft) {
                                // Flush edits into draft before switching
                                const flushed = flushDraftEdits();
                                if (flushed) setDraftSkill(flushed);
                              } else if (isDirty) {
                                setPendingFile(f.path);
                                return;
                              }
                              setEditedContent(null);
                              setEditedDescription(null);
                              setEditedBody(null);
                              setActiveFile(f.path);
                            }}
                          >
                            {f.path}
                          </span>
                          {!isImported && (
                            <div className="hidden group-hover:flex items-center gap-0.5 shrink-0">
                              {f.path !== 'SKILL.md' && (
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <button
                                      className="p-0.5 hover:text-primary"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setRenamingFile(f.path);
                                        setRenameFileValue(f.path);
                                      }}
                                    >
                                      <PencilLine className="h-2.5 w-2.5" />
                                    </button>
                                  </TooltipTrigger>
                                  <TooltipContent>Rename</TooltipContent>
                                </Tooltip>
                              )}
                              {f.path !== 'SKILL.md' && (
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <button
                                      className="p-0.5 hover:text-destructive"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setDeletingFile(f.path);
                                      }}
                                    >
                                      <X className="h-2.5 w-2.5" />
                                    </button>
                                  </TooltipTrigger>
                                  <TooltipContent>Delete</TooltipContent>
                                </Tooltip>
                              )}
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  ))}
                  {addingFile && (
                    <div className="px-3 py-1">
                      <input
                        className="w-full bg-transparent border-b border-primary text-xs outline-none"
                        placeholder="file-path.md"
                        value={newFilePath}
                        onChange={(e) => setNewFilePath(e.target.value)}
                        onBlur={() => { if (!newFilePath.trim()) setAddingFile(false); else handleAddFile(); }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') handleAddFile();
                          if (e.key === 'Escape') { setAddingFile(false); setNewFilePath(''); }
                        }}
                        autoFocus
                      />
                    </div>
                  )}
                </div>
              </div>

              {/* Editor content area */}
              <div className="flex-1 flex flex-col min-w-0 p-4 overflow-y-auto">
                {isImported && (
                  <div className="mb-3 px-3 py-2 bg-muted/50 border rounded-md flex items-center gap-2 text-xs text-muted-foreground">
                    <Lock className="h-3 w-3 shrink-0" />
                    <span>This skill is imported from{' '}
                      {detail?.source_type === 'github' && detail?.source_repo ? (
                        <a
                          href={`https://github.com/${detail.source_repo}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="font-semibold text-foreground hover:underline inline-flex items-center gap-0.5"
                        >
                          {detail.source_repo}
                          <ExternalLink className="h-2.5 w-2.5" />
                        </a>
                      ) : (
                        <strong>{detail?.source_repo}</strong>
                      )}
                      . It&apos;s read-only. Use &quot;Copy to Edit&quot; to create an editable version.</span>
                  </div>
                )}
                {isSkillMd ? (
                  <div className="space-y-4 max-w-2xl">
                    <div className="space-y-2">
                      <Label className="text-xs font-medium text-muted-foreground">Description</Label>
                      <Input
                        placeholder="Brief description of what this skill does..."
                        value={displayDescription}
                        onChange={(e) => {
                          setEditedDescription(e.target.value);
                          const d = draftSkill ?? ensureEditDraft();
                          if (d) {
                            const content = composeSkillMd(d.name, e.target.value, displayBody);
                            setDraftSkill({ ...d, files: d.files.map((f) => f.path === 'SKILL.md' ? { ...f, content: content } : f) });
                          }
                        }}
                        className="text-sm"
                        readOnly={isImported}
                      />
                    </div>
                    <div className="space-y-2 flex-1">
                      <Label className="text-xs font-medium text-muted-foreground">Skill Instructions</Label>
                      <Textarea
                        placeholder="Write the skill instructions here..."
                        value={displayBody}
                        onChange={(e) => {
                          setEditedBody(e.target.value);
                          const d = draftSkill ?? ensureEditDraft();
                          if (d) {
                            const content = composeSkillMd(d.name, displayDescription, e.target.value);
                            setDraftSkill({ ...d, files: d.files.map((f) => f.path === 'SKILL.md' ? { ...f, content: content } : f) });
                          }
                        }}
                        className="font-mono text-xs min-h-[400px] resize-y"
                        readOnly={isImported}
                      />
                    </div>
                  </div>
                ) : (
                  <Textarea
                    className="flex-1 font-mono text-xs min-h-[400px] resize-y"
                    value={displayContent}
                    onChange={(e) => {
                      setEditedContent(e.target.value);
                      const d = draftSkill ?? ensureEditDraft();
                      if (d) {
                        setDraftSkill({ ...d, files: d.files.map((f) => f.path === activeFile ? { ...f, content: e.target.value } : f) });
                      }
                    }}
                    placeholder="File content..."
                    readOnly={isImported}
                  />
                )}
              </div>
            </div>
          </>
        ))}
      </div>

      {/* Delete file confirmation */}
      {/* Discard unsaved changes when switching files */}
      <AlertDialog open={!!pendingFile} onOpenChange={(o) => { if (!o) setPendingFile(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              You have unsaved edits in <strong>{activeFile}</strong>. Switching files will
              discard them.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingFile) {
                  setEditedContent(null);
                  setEditedDescription(null);
                  setEditedBody(null);
                  setActiveFile(pendingFile);
                }
                setPendingFile(null);
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Discard changes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={!!deletingFile} onOpenChange={() => setDeletingFile(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete file?</AlertDialogTitle>
            <AlertDialogDescription>
              This will permanently delete <strong>{deletingFile}</strong> from this skill.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteFile}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete skill confirmation */}
      <AlertDialog open={!!deletingSkill} onOpenChange={() => setDeletingSkill(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete skill from registry?</AlertDialogTitle>
            <AlertDialogDescription>
              This will permanently remove <strong>{deletingSkill?.name}</strong> from the registry.
              Existing activations will stop receiving updates.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (deletingSkill?.id) {
                  deleteMutation.mutate({ path: { skill_id: deletingSkill.id } });
                }
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Update diff dialog */}
      <Dialog open={!!updateDiff} onOpenChange={() => setUpdateDiff(null)}>
        <DialogContent className="sm:max-w-4xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>Upstream Changes Available</DialogTitle>
            <DialogDescription>
              The source repository has new changes. Review the diff below and apply if desired.
            </DialogDescription>
          </DialogHeader>
          <div className="flex-1 overflow-y-auto border rounded-md">
            {updateDiff?.files.map((fileDiff) => (
              <div key={fileDiff.path} className="border-b last:border-b-0">
                <div className="px-3 py-1.5 bg-muted/50 border-b flex items-center gap-2 text-xs font-medium sticky top-0">
                  <FileText className="h-3 w-3" />
                  <span>{fileDiff.path}</span>
                  <Badge variant={fileDiff.status === 'added' ? 'default' : fileDiff.status === 'removed' ? 'destructive' : 'secondary'} className="text-[9px] px-1 py-0">
                    {fileDiff.status}
                  </Badge>
                </div>
                <ReactDiffViewer
                  oldValue={fileDiff.current ?? ''}
                  newValue={fileDiff.latest ?? ''}
                  splitView={false}
                  compareMethod={DiffMethod.LINES}
                  useDarkTheme={document.documentElement.classList.contains('dark')}
                  hideLineNumbers={false}
                  styles={{ contentText: { fontSize: '11px', lineHeight: '1.4' } }}
                />
              </div>
            ))}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setUpdateDiff(null)}>Cancel</Button>
            <Button onClick={handleApplyUpdate} disabled={applyingUpdate}>
              {applyingUpdate && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />}
              Apply Update
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Version history dialog */}
      <Dialog open={showHistory} onOpenChange={(open) => { if (!open) { setShowHistory(false); setSelectedVersion(null); } }}>
        <DialogContent className="sm:max-w-4xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>Version History</DialogTitle>
            <DialogDescription>
              Each version represents a unique content snapshot identified by its hash.
            </DialogDescription>
          </DialogHeader>
          {loadingHistory ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : selectedVersion ? (
            <div className="flex-1 flex flex-col min-h-0 gap-3">
              <div className="flex items-center justify-between">
                <button
                  className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
                  onClick={() => setSelectedVersion(null)}
                >
                  ← Back to list
                </button>
                <div className="flex items-center gap-2">
                  <code className="text-[10px] bg-muted px-1.5 py-0.5 rounded">{selectedVersion.content_hash.slice(0, 12)}</code>
                  <span className="text-[10px] text-muted-foreground">
                    {new Date(selectedVersion.created_at).toLocaleString()}
                  </span>
                </div>
              </div>
              <div className="flex-1 overflow-y-auto border rounded-md">
                {(() => {
                  const prevFiles = selectedVersion.prevFiles;
                  // Collect all unique file paths from both this version and previous
                  const allPaths = new Set([
                    ...selectedVersion.files.map((f) => f.path),
                    ...(prevFiles ?? []).map((f) => f.path),
                  ]);
                  return Array.from(allPaths).map((path) => {
                    const thisFile = selectedVersion.files.find((f) => f.path === path);
                    const prevFile = prevFiles?.find((f) => f.path === path);
                    const thisContent = thisFile?.content ?? '';
                    const prevContent = prevFile?.content ?? '';
                    const isSame = thisContent === prevContent;
                    const isNew = !prevFile && !!thisFile;
                    const isDeleted = !!prevFile && !thisFile;
                    return (
                      <div key={path} className="border-b last:border-b-0">
                        <div className="px-3 py-1.5 bg-muted/50 border-b flex items-center gap-2 text-xs font-medium sticky top-0">
                          <FileText className="h-3 w-3" />
                          <span>{path}</span>
                          {isNew && <Badge variant="default" className="text-[9px] px-1 py-0">added</Badge>}
                          {isDeleted && <Badge variant="destructive" className="text-[9px] px-1 py-0">removed</Badge>}
                          {!isNew && !isDeleted && !isSame && <Badge variant="secondary" className="text-[9px] px-1 py-0">changed</Badge>}
                          {isSame && <Badge variant="outline" className="text-[9px] px-1 py-0">unchanged</Badge>}
                        </div>
                        {!isSame && (
                          <ReactDiffViewer
                            oldValue={prevContent}
                            newValue={thisContent}
                            splitView={false}
                            compareMethod={DiffMethod.LINES}
                            useDarkTheme={document.documentElement.classList.contains('dark')}
                            hideLineNumbers={false}
                            leftTitle={prevFiles ? 'Previous version' : '(empty)'}
                            rightTitle="This version"
                            styles={{ contentText: { fontSize: '11px', lineHeight: '1.4' } }}
                          />
                        )}
                      </div>
                    );
                  });
                })()}
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setSelectedVersion(null)}>Back</Button>
                {!isImported && (
                  <Button onClick={handleRestoreVersion} disabled={restoringVersion}>
                    {restoringVersion ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" /> : <RotateCcw className="h-3.5 w-3.5 mr-1.5" />}
                    Restore This Version
                  </Button>
                )}
              </DialogFooter>
            </div>
          ) : versionHistory.length === 0 ? (
            <div className="text-center text-muted-foreground py-8">
              <History className="h-8 w-8 mx-auto mb-2 opacity-30" />
              <p className="text-sm">No version history available</p>
            </div>
          ) : (
            <div className="flex-1 overflow-y-auto border rounded-md divide-y">
              {versionHistory.map((v, i) => (
                <button
                  key={v.content_hash}
                  className="w-full text-left px-4 py-3 hover:bg-accent/50 transition-colors flex items-center gap-3"
                  onClick={() => handleViewVersion(v.content_hash)}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <code className="text-xs font-mono bg-muted px-1.5 py-0.5 rounded">{v.content_hash.slice(0, 12)}</code>
                      {i === 0 && <Badge variant="outline" className="text-[9px] px-1 py-0">current</Badge>}
                    </div>
                    {v.description && (
                      <p className="text-xs text-muted-foreground mt-1 truncate">{v.description}</p>
                    )}
                  </div>
                  <span className="text-[10px] text-muted-foreground whitespace-nowrap">
                    {new Date(v.created_at).toLocaleString()}
                  </span>
                </button>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
