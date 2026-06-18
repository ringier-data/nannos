import { useState, useEffect, useCallback, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  File,
  FileText,
  Plus,
  Trash2,
  Pencil,
  Save,
  Loader2,
  FolderOpen,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  getSkillApiV1PlaybooksAgentsAgentNameSkillsSkillNameGetOptions,
  getSkillApiV1PlaybooksAgentsAgentNameSkillsSkillNameGetQueryKey,
  updateSkillApiV1PlaybooksAgentsAgentNameSkillsScopeSkillNamePutMutation,
  listSkillsApiV1PlaybooksAgentsAgentNameSkillsGetQueryKey,
} from '@/api/generated/@tanstack/react-query.gen';
import { listSkillFiles, getSkillFile, writeSkillFile, deleteSkillFile } from '@/api/skill-files';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
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
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

/** Parse SKILL.md content into frontmatter fields + body */
function parseSkillContent(raw: string): { description: string; body: string } {
  const trimmed = raw.trim();
  if (!trimmed.startsWith('---')) {
    return { description: '', body: trimmed };
  }
  const endIdx = trimmed.indexOf('---', 3);
  if (endIdx === -1) {
    return { description: '', body: trimmed };
  }
  const frontmatter = trimmed.slice(3, endIdx);
  const body = trimmed.slice(endIdx + 3).replace(/^\n+/, '');

  let description = '';
  for (const line of frontmatter.split('\n')) {
    const match = line.match(/^description:\s*(.*)$/);
    if (match) {
      description = match[1].trim();
    }
  }
  return { description, body };
}

/** Compose SKILL.md content from structured fields */
function composeSkillContent(name: string, description: string, body: string): string {
  const lines = ['---', `name: ${name}`];
  if (description) lines.push(`description: ${description}`);
  lines.push('---', '');
  if (body) lines.push(body);
  let content = lines.join('\n');
  if (!content.endsWith('\n')) content += '\n';
  return content;
}

interface SkillEditorPanelProps {
  agentName: string;
  skillName: string;
  scope: string;
  groupId?: string;
  onClose?: () => void;
}

export function SkillEditorPanel({
  agentName,
  skillName,
  scope,
  groupId,
  onClose: _onClose,
}: SkillEditorPanelProps) {
  const queryClient = useQueryClient();

  // Active file in the editor
  const [activeFile, setActiveFile] = useState<string>('SKILL.md');
  const [editedContent, setEditedContent] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Structured SKILL.md editing
  const [editedDescription, setEditedDescription] = useState<string | null>(null);
  const [editedBody, setEditedBody] = useState<string | null>(null);

  // Add file dialog
  const [showAddFile, setShowAddFile] = useState(false);
  const [newFilePath, setNewFilePath] = useState('');

  // Delete file confirmation
  const [deletingFile, setDeletingFile] = useState<string | null>(null);

  // Inline file rename state
  const [renamingFile, setRenamingFile] = useState<string | null>(null);
  const [renameFileValue, setRenameFileValue] = useState('');

  // Fetch SKILL.md content
  const { data: skillDetail, isLoading: skillLoading } = useQuery({
    ...getSkillApiV1PlaybooksAgentsAgentNameSkillsSkillNameGetOptions({
      path: { agent_name: agentName, skill_name: skillName },
      query: { scope, group_id: groupId },
    }),
  });

  // Fetch file list
  const {
    data: files,
    isLoading: filesLoading,
    refetch: refetchFiles,
  } = useQuery({
    queryKey: ['skill-files', agentName, skillName, scope, groupId],
    queryFn: () => listSkillFiles(agentName, skillName, scope, groupId),
  });

  // Fetch content for non-SKILL.md files
  const [fileContents, setFileContents] = useState<Record<string, string>>({});
  const [loadingFile, setLoadingFile] = useState(false);

  // Load file content when active file changes
  useEffect(() => {
    if (activeFile === 'SKILL.md') {
      setEditedContent(null);
      return;
    }
    // Reset structured fields when leaving SKILL.md
    setEditedDescription(null);
    setEditedBody(null);
    if (fileContents[activeFile] !== undefined) {
      setEditedContent(null);
      return;
    }
    let cancelled = false;
    setLoadingFile(true);
    getSkillFile(agentName, skillName, activeFile, scope, groupId)
      .then((content) => {
        if (!cancelled) {
          setFileContents((prev) => ({ ...prev, [activeFile]: content }));
          setLoadingFile(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          toast.error(`Failed to load ${activeFile}`);
          setLoadingFile(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [activeFile, agentName, skillName, scope, groupId, fileContents]);

  // Parse SKILL.md into structured fields
  const serverSkillRaw = skillDetail?.content ?? '';
  const parsedSkill = useMemo(() => parseSkillContent(serverSkillRaw), [serverSkillRaw]);

  // Current values for SKILL.md structured fields
  const currentDescription = editedDescription ?? parsedSkill.description;
  const currentBody = editedBody ?? parsedSkill.body;
  const hasSkillChanges = editedDescription !== null || editedBody !== null;

  // Current content for non-SKILL.md files
  const serverContent = fileContents[activeFile] ?? '';
  const displayContent = editedContent ?? serverContent;
  const hasFileChanges = editedContent !== null;

  // Combined dirty state
  const hasChanges = activeFile === 'SKILL.md' ? hasSkillChanges : hasFileChanges;

  // Update SKILL.md mutation
  const updateSkillMutation = useMutation({
    ...updateSkillApiV1PlaybooksAgentsAgentNameSkillsScopeSkillNamePutMutation(),
    onSuccess: () => {
      toast.success('SKILL.md saved');
      queryClient.invalidateQueries({
        queryKey: getSkillApiV1PlaybooksAgentsAgentNameSkillsSkillNameGetQueryKey({
          path: { agent_name: agentName, skill_name: skillName },
          query: { scope, group_id: groupId },
        }),
      });
      queryClient.invalidateQueries({
        queryKey: listSkillsApiV1PlaybooksAgentsAgentNameSkillsGetQueryKey({
          path: { agent_name: agentName },
        }),
      });
      setEditedDescription(null);
      setEditedBody(null);
    },
    onError: () => toast.error('Failed to save SKILL.md'),
  });

  // Save the current file
  const handleSave = useCallback(async () => {
    if (!hasChanges) return;

    if (activeFile === 'SKILL.md') {
      const composed = composeSkillContent(skillName, currentDescription, currentBody);
      updateSkillMutation.mutate({
        path: { agent_name: agentName, scope, skill_name: skillName },
        query: { group_id: groupId },
        body: { content: composed },
      });
      return;
    }

    // Non-SKILL.md file
    setSaving(true);
    try {
      await writeSkillFile(agentName, skillName, activeFile, displayContent, scope, groupId);
      setFileContents((prev) => ({ ...prev, [activeFile]: displayContent }));
      setEditedContent(null);
      toast.success(`${activeFile} saved`);
    } catch {
      toast.error(`Failed to save ${activeFile}`);
    } finally {
      setSaving(false);
    }
  }, [activeFile, agentName, skillName, scope, groupId, displayContent, hasChanges, updateSkillMutation, currentDescription, currentBody]);

  // Add new file
  const handleAddFile = useCallback(async () => {
    const path = newFilePath.trim();
    if (!path) return;

    // Basic validation
    if (path.startsWith('/') || path.includes('..') || path === 'SKILL.md') {
      toast.error('Invalid file path');
      return;
    }
    const segments = path.split('/');
    if (segments.length > 3 || segments.some((s) => !s)) {
      toast.error('Path exceeds max depth (3 segments) or has empty segments');
      return;
    }

    setSaving(true);
    try {
      await writeSkillFile(agentName, skillName, path, '', scope, groupId);
      setFileContents((prev) => ({ ...prev, [path]: '' }));
      await refetchFiles();
      setActiveFile(path);
      setShowAddFile(false);
      setNewFilePath('');
      toast.success(`Created ${path}`);
    } catch {
      toast.error(`Failed to create ${path}`);
    } finally {
      setSaving(false);
    }
  }, [agentName, skillName, scope, groupId, newFilePath, refetchFiles]);

  // Delete file
  const handleDeleteFile = useCallback(async () => {
    if (!deletingFile) return;
    setSaving(true);
    try {
      await deleteSkillFile(agentName, skillName, deletingFile, scope, groupId);
      setFileContents((prev) => {
        const next = { ...prev };
        delete next[deletingFile];
        return next;
      });
      await refetchFiles();
      if (activeFile === deletingFile) setActiveFile('SKILL.md');
      setDeletingFile(null);
      toast.success(`Deleted ${deletingFile}`);
    } catch {
      toast.error(`Failed to delete ${deletingFile}`);
    } finally {
      setSaving(false);
    }
  }, [agentName, skillName, scope, groupId, deletingFile, activeFile, refetchFiles]);

  const isReadOnly = scope === 'standard';
  const fileList = files ?? [];
  const allFiles = ['SKILL.md', ...fileList.map((f) => f.path)];

  return (
    <div className="flex h-full min-h-[400px]">
      {/* File Tree Sidebar */}
      <div className="w-56 shrink-0 border-r flex flex-col bg-muted/30">
        <div className="flex items-center justify-between px-3 py-2 border-b">
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Files
          </span>
          {!isReadOnly && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 w-6 p-0"
                  onClick={() => setShowAddFile(true)}
                >
                  <Plus className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Add file</TooltipContent>
            </Tooltip>
          )}
        </div>

        <div className="flex-1 overflow-y-auto py-1">
          {skillLoading || filesLoading ? (
            <div className="px-3 py-2 text-xs text-muted-foreground">Loading...</div>
          ) : (
            allFiles.map((filePath) => {
              const isActive = filePath === activeFile;
              const isSKILL = filePath === 'SKILL.md';
              return (
                <div
                  key={filePath}
                  className={`group flex items-center gap-1.5 px-3 py-1.5 text-sm cursor-pointer hover:bg-accent ${
                    isActive ? 'bg-accent text-accent-foreground' : ''
                  }`}
                  onClick={() => {
                    if (hasChanges) {
                      setEditedContent(null);
                      setEditedDescription(null);
                      setEditedBody(null);
                    }
                    setActiveFile(filePath);
                  }}
                >
                  {isSKILL ? (
                    <FileText className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                  ) : (
                    <File className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                  )}
                  {renamingFile === filePath ? (
                    <Input
                      value={renameFileValue}
                      onChange={(e) => setRenameFileValue(e.target.value)}
                      onBlur={async () => {
                        const newPath = renameFileValue.trim();
                        if (newPath && newPath !== filePath) {
                          setSaving(true);
                          try {
                            const content = fileContents[filePath] ?? '';
                            await writeSkillFile(agentName, skillName, newPath, content, scope, groupId);
                            await deleteSkillFile(agentName, skillName, filePath, scope, groupId);
                            setFileContents((prev) => {
                              const next = { ...prev };
                              next[newPath] = content;
                              delete next[filePath];
                              return next;
                            });
                            if (activeFile === filePath) setActiveFile(newPath);
                            await refetchFiles();
                            toast.success(`Renamed to ${newPath}`);
                          } catch {
                            toast.error('Failed to rename file');
                          } finally {
                            setSaving(false);
                          }
                        }
                        setRenamingFile(null);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          (e.target as HTMLInputElement).blur();
                        } else if (e.key === 'Escape') {
                          setRenamingFile(null);
                        }
                      }}
                      className="h-5 flex-1 font-mono text-xs px-1 py-0"
                      autoFocus
                      onClick={(e) => e.stopPropagation()}
                    />
                  ) : (
                    <span className="truncate flex-1 font-mono text-xs">{filePath}</span>
                  )}
                  {!isSKILL && !isReadOnly && renamingFile !== filePath && (
                    <>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-5 w-5 p-0 opacity-0 group-hover:opacity-100"
                        onClick={(e) => {
                          e.stopPropagation();
                          setRenamingFile(filePath);
                          setRenameFileValue(filePath);
                        }}
                      >
                        <Pencil className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-5 w-5 p-0 opacity-0 group-hover:opacity-100"
                        onClick={(e) => {
                          e.stopPropagation();
                          setDeletingFile(filePath);
                        }}
                      >
                        <Trash2 className="h-3 w-3 text-destructive" />
                      </Button>
                    </>
                  )}
                </div>
              );
            })
          )}
          {!skillLoading && !filesLoading && allFiles.length === 1 && (
            <div className="px-3 py-4 text-xs text-muted-foreground text-center">
              <FolderOpen className="h-5 w-5 mx-auto mb-1 opacity-50" />
              No bundled files yet.
              {!isReadOnly && <p className="mt-1">Click + to add files.</p>}
            </div>
          )}
        </div>
      </div>

      {/* Editor Pane */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Editor toolbar */}
        <div className="flex items-center justify-between px-4 py-2 border-b bg-background">
          <span className="font-mono text-sm text-muted-foreground truncate">{activeFile}</span>
          {!isReadOnly && (
            <div className="flex items-center gap-2 shrink-0">
              {hasChanges && (
                <Button variant="ghost" size="sm" onClick={() => {
                  setEditedContent(null);
                  setEditedDescription(null);
                  setEditedBody(null);
                }}>
                  Discard
                </Button>
              )}
              <Button
                size="sm"
                disabled={!hasChanges || saving || updateSkillMutation.isPending}
                onClick={handleSave}
              >
                {saving || updateSkillMutation.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Save className="mr-2 h-4 w-4" />
                )}
                Save
              </Button>
            </div>
          )}
        </div>

        {/* Editor content */}
        <div className="flex-1 p-4 overflow-auto">
          {loadingFile || skillLoading ? (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin mr-2" />
              Loading...
            </div>
          ) : activeFile === 'SKILL.md' ? (
            /* Structured SKILL.md editor */
            <div className="flex flex-col gap-4 h-full">
              <div>
                <Label className="text-sm font-medium">Description</Label>
                <Input
                  value={currentDescription}
                  onChange={(e) => setEditedDescription(e.target.value)}
                  className="mt-1"
                  readOnly={isReadOnly}
                  placeholder="What this skill does and when to use it"
                />
              </div>
              <div className="flex-1 flex flex-col min-h-0">
                <Label className="text-sm font-medium mb-1">Instructions (Markdown)</Label>
                <Textarea
                  value={currentBody}
                  onChange={(e) => setEditedBody(e.target.value)}
                  className="flex-1 min-h-[200px] font-mono text-sm resize-none"
                  readOnly={isReadOnly}
                  placeholder="## Steps&#10;&#10;1. Check monitoring dashboards&#10;2. Identify affected services"
                />
              </div>
            </div>
          ) : (
            /* Raw text editor for other files */
            <Textarea
              value={displayContent}
              onChange={(e) => setEditedContent(e.target.value)}
              className="w-full h-full min-h-[300px] font-mono text-sm resize-none"
              readOnly={isReadOnly}
              placeholder="File content..."
            />
          )}
        </div>
      </div>

      {/* Add File Dialog */}
      <Dialog open={showAddFile} onOpenChange={setShowAddFile}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Add File to Skill</DialogTitle>
          </DialogHeader>
          <div className="py-2">
            <Input
              value={newFilePath}
              onChange={(e) => setNewFilePath(e.target.value)}
              placeholder="scripts/validate.py"
              className="font-mono"
            />
            <p className="text-xs text-muted-foreground mt-2">
              Relative path (max 3 segments). Examples: <code>config.json</code>,{' '}
              <code>scripts/check.py</code>
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowAddFile(false)}>
              Cancel
            </Button>
            <Button onClick={handleAddFile} disabled={!newFilePath.trim() || saving}>
              {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plus className="mr-2 h-4 w-4" />}
              Add
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete File Confirmation */}
      <AlertDialog open={!!deletingFile} onOpenChange={(open) => !open && setDeletingFile(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete file &quot;{deletingFile}&quot;?</AlertDialogTitle>
            <AlertDialogDescription>
              This will remove the file from the skill folder. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteFile}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Trash2 className="mr-2 h-4 w-4" />}
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
