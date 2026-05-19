import { useState, useCallback } from 'react';
import {
  File,
  FileText,
  Plus,
  Trash2,
  Save,
  Pencil,
  FolderOpen,
} from 'lucide-react';
import type { SkillDefinitionInput as SkillDefinition } from '@/api/generated/types.gen';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

interface SkillEditorModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  skills: SkillDefinition[];
  onChange: (skills: SkillDefinition[]) => void;
  disabled?: boolean;
}

/**
 * Full-screen modal for editing default-scope skills.
 * Works entirely with local state — changes are applied via onChange callback.
 */
export function SkillEditorModal({
  open,
  onOpenChange,
  skills: externalSkills,
  onChange,
  disabled,
}: SkillEditorModalProps) {
  // Local copy for editing
  const [localSkills, setLocalSkills] = useState<SkillDefinition[]>(() =>
    JSON.parse(JSON.stringify(externalSkills)),
  );
  const [activeSkillIdx, setActiveSkillIdx] = useState<number | null>(
    externalSkills.length > 0 ? 0 : null,
  );
  const [activeFile, setActiveFile] = useState<string>('SKILL.md');

  // Add file dialog
  const [showAddFile, setShowAddFile] = useState(false);
  const [newFilePath, setNewFilePath] = useState('');

  // Add skill dialog
  const [showAddSkill, setShowAddSkill] = useState(false);
  const [newSkillName, setNewSkillName] = useState('');
  const [newSkillDesc, setNewSkillDesc] = useState('');

  // Inline rename state
  const [renamingIdx, setRenamingIdx] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState('');

  // Inline file rename state
  const [renamingFile, setRenamingFile] = useState<string | null>(null);
  const [renameFileValue, setRenameFileValue] = useState('');

  const activeSkill = activeSkillIdx !== null ? localSkills[activeSkillIdx] : null;

  const updateSkill = useCallback(
    (idx: number, patch: Partial<SkillDefinition>) => {
      setLocalSkills((prev) => {
        const updated = [...prev];
        updated[idx] = { ...updated[idx], ...patch };
        return updated;
      });
    },
    [],
  );

  const handleDeleteSkill = useCallback(
    (idx: number) => {
      setLocalSkills((prev) => prev.filter((_, i) => i !== idx));
      if (activeSkillIdx === idx) {
        setActiveSkillIdx(null);
        setActiveFile('SKILL.md');
      } else if (activeSkillIdx !== null && activeSkillIdx > idx) {
        setActiveSkillIdx(activeSkillIdx - 1);
      }
    },
    [activeSkillIdx],
  );

  const handleAddSkill = useCallback(() => {
    if (!newSkillName.trim()) return;
    const skill: SkillDefinition = {
      name: newSkillName,
      description: newSkillDesc,
      body: '',
    };
    setLocalSkills((prev) => [...prev, skill]);
    setActiveSkillIdx(localSkills.length);
    setActiveFile('SKILL.md');
    setShowAddSkill(false);
    setNewSkillName('');
    setNewSkillDesc('');
  }, [newSkillName, newSkillDesc, localSkills.length]);

  const handleAddFile = useCallback(() => {
    if (activeSkillIdx === null || !newFilePath.trim()) return;
    const path = newFilePath.trim();
    setLocalSkills((prev) => {
      const updated = [...prev];
      const files = [...(updated[activeSkillIdx].files ?? []), { path, content: '' }];
      updated[activeSkillIdx] = { ...updated[activeSkillIdx], files };
      return updated;
    });
    setActiveFile(path);
    setShowAddFile(false);
    setNewFilePath('');
  }, [activeSkillIdx, newFilePath]);

  const handleDeleteFile = useCallback(
    (filePath: string) => {
      if (activeSkillIdx === null) return;
      setLocalSkills((prev) => {
        const updated = [...prev];
        const files = (updated[activeSkillIdx].files ?? []).filter((f: { path: string }) => f.path !== filePath);
        updated[activeSkillIdx] = {
          ...updated[activeSkillIdx],
          files: files.length > 0 ? files : undefined,
        };
        return updated;
      });
      if (activeFile === filePath) setActiveFile('SKILL.md');
    },
    [activeSkillIdx, activeFile],
  );

  const handleSave = () => {
    onChange(localSkills);
    onOpenChange(false);
  };

  // Get content for active file of active skill
  const getActiveContent = (): string => {
    if (!activeSkill) return '';
    if (activeFile === 'SKILL.md') return activeSkill.body ?? '';
    return activeSkill.files?.find((f: { path: string; content: string }) => f.path === activeFile)?.content ?? '';
  };

  const setActiveContent = (content: string) => {
    if (activeSkillIdx === null) return;
    if (activeFile === 'SKILL.md') {
      updateSkill(activeSkillIdx, { body: content });
      return;
    }
    setLocalSkills((prev) => {
      const updated = [...prev];
      const files = (updated[activeSkillIdx].files ?? []).map((f: { path: string; content: string }) =>
        f.path === activeFile ? { ...f, content } : f,
      );
      updated[activeSkillIdx] = { ...updated[activeSkillIdx], files };
      return updated;
    });
  };

  const activeFiles: string[] = activeSkill
    ? ['SKILL.md', ...(activeSkill.files ?? []).map((f: { path: string }) => f.path)]
    : [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-6xl h-[80vh] flex flex-col p-0 gap-0">
        <DialogHeader className="px-6 py-4 border-b shrink-0">
          <DialogTitle>Edit Standard Skills</DialogTitle>
        </DialogHeader>

        <div className="flex flex-1 min-h-0">
          {/* Skill List */}
          <div className="w-52 shrink-0 border-r flex flex-col bg-muted/30">
            <div className="flex items-center justify-between px-3 py-2 border-b">
              <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Skills
              </span>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 w-6 p-0"
                onClick={() => setShowAddSkill(true)}
                disabled={disabled}
              >
                <Plus className="h-3.5 w-3.5" />
              </Button>
            </div>
            <div className="flex-1 overflow-y-auto py-1">
              {localSkills.length === 0 ? (
                <div className="px-3 py-4 text-xs text-muted-foreground text-center">
                  No skills. Click + to add.
                </div>
              ) : (
                localSkills.map((skill, idx) => (
                  <div
                    key={idx}
                    className={`group flex items-center gap-1.5 px-3 py-1.5 cursor-pointer hover:bg-accent ${
                      activeSkillIdx === idx ? 'bg-accent text-accent-foreground' : ''
                    }`}
                    onClick={() => {
                      setActiveSkillIdx(idx);
                      setActiveFile('SKILL.md');
                    }}
                  >
                    {renamingIdx === idx ? (
                      <Input
                        value={renameValue}
                        onChange={(e) =>
                          setRenameValue(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))
                        }
                        onBlur={() => {
                          if (renameValue.trim()) {
                            updateSkill(idx, { name: renameValue });
                          }
                          setRenamingIdx(null);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            if (renameValue.trim()) {
                              updateSkill(idx, { name: renameValue });
                            }
                            setRenamingIdx(null);
                          } else if (e.key === 'Escape') {
                            setRenamingIdx(null);
                          }
                        }}
                        className="h-5 flex-1 font-mono text-xs px-1 py-0"
                        autoFocus
                        onClick={(e) => e.stopPropagation()}
                      />
                    ) : (
                      <span className="truncate flex-1 font-mono text-xs">{skill.name || '(unnamed)'}</span>
                    )}
                    {(skill.files?.length ?? 0) > 0 && renamingIdx !== idx && (
                      <span className="text-[10px] text-muted-foreground">{skill.files!.length}f</span>
                    )}
                    {!disabled && renamingIdx !== idx && (
                      <>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-5 w-5 p-0 opacity-0 group-hover:opacity-100"
                          onClick={(e) => {
                            e.stopPropagation();
                            setRenamingIdx(idx);
                            setRenameValue(skill.name);
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
                            handleDeleteSkill(idx);
                          }}
                        >
                          <Trash2 className="h-3 w-3 text-destructive" />
                        </Button>
                      </>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>

          {/* File Tree + Editor */}
          {activeSkill ? (
            <>
              {/* File tree for active skill */}
              <div className="w-48 shrink-0 border-r flex flex-col bg-muted/20">
                <div className="flex items-center justify-between px-3 py-2 border-b">
                  <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                    Files
                  </span>
                  {!disabled && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 w-6 p-0"
                      onClick={() => setShowAddFile(true)}
                    >
                      <Plus className="h-3.5 w-3.5" />
                    </Button>
                  )}
                </div>
                <div className="flex-1 overflow-y-auto py-1">
                  {activeFiles.map((fp) => {
                    const isSKILL = fp === 'SKILL.md';
                    return (
                      <div
                        key={fp}
                        className={`group flex items-center gap-1.5 px-3 py-1.5 text-sm cursor-pointer hover:bg-accent ${
                          activeFile === fp ? 'bg-accent text-accent-foreground' : ''
                        }`}
                        onClick={() => setActiveFile(fp)}
                      >
                        {isSKILL ? (
                          <FileText className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                        ) : (
                          <File className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                        )}
                        {renamingFile === fp ? (
                          <Input
                            value={renameFileValue}
                            onChange={(e) => setRenameFileValue(e.target.value)}
                            onBlur={() => {
                              if (renameFileValue.trim() && renameFileValue !== fp && activeSkillIdx !== null) {
                                setLocalSkills((prev) => {
                                  const updated = [...prev];
                                  const files = (updated[activeSkillIdx].files ?? []).map((f: { path: string; content: string }) =>
                                    f.path === fp ? { ...f, path: renameFileValue.trim() } : f,
                                  );
                                  updated[activeSkillIdx] = { ...updated[activeSkillIdx], files };
                                  return updated;
                                });
                                if (activeFile === fp) setActiveFile(renameFileValue.trim());
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
                          <span className="truncate flex-1 font-mono text-xs">{fp}</span>
                        )}
                        {!isSKILL && !disabled && renamingFile !== fp && (
                          <>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-5 w-5 p-0 opacity-0 group-hover:opacity-100"
                              onClick={(e) => {
                                e.stopPropagation();
                                setRenamingFile(fp);
                                setRenameFileValue(fp);
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
                                handleDeleteFile(fp);
                              }}
                            >
                              <Trash2 className="h-3 w-3 text-destructive" />
                            </Button>
                          </>
                        )}
                      </div>
                    );
                  })}
                  {activeFiles.length === 1 && (
                    <div className="px-3 py-3 text-xs text-muted-foreground text-center">
                      <FolderOpen className="h-4 w-4 mx-auto mb-1 opacity-50" />
                      No bundled files
                    </div>
                  )}
                </div>


              </div>

              {/* Editor */}
              <div className="flex-1 flex flex-col min-w-0">
                <div className="flex items-center px-4 py-2 border-b bg-background">
                  <span className="font-mono text-sm text-muted-foreground">{activeFile}</span>
                </div>
                {activeFile === 'SKILL.md' ? (
                  /* Structured SKILL.md editor */
                  <div className="flex-1 flex flex-col gap-4 p-4 overflow-auto">
                    <div>
                      <Label className="text-sm font-medium">Description</Label>
                      <Input
                        value={activeSkill.description}
                        onChange={(e) =>
                          updateSkill(activeSkillIdx!, { description: e.target.value })
                        }
                        className="mt-1"
                        disabled={disabled}
                        placeholder="What this skill does and when to use it"
                      />
                    </div>
                    <div className="flex-1 flex flex-col min-h-0">
                      <Label className="text-sm font-medium mb-1">Instructions (Markdown)</Label>
                      <Textarea
                        value={getActiveContent()}
                        onChange={(e) => setActiveContent(e.target.value)}
                        className="flex-1 min-h-[200px] font-mono text-sm resize-none"
                        disabled={disabled}
                        placeholder="## Steps&#10;&#10;1. Step one&#10;2. Step two"
                      />
                    </div>
                  </div>
                ) : (
                  /* Raw text editor for other files */
                  <div className="flex-1 p-4 overflow-auto">
                    <Textarea
                      value={getActiveContent()}
                      onChange={(e) => setActiveContent(e.target.value)}
                      className="w-full h-full min-h-[200px] font-mono text-sm resize-none"
                      disabled={disabled}
                      placeholder="File content..."
                    />
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="flex-1 flex items-center justify-center text-muted-foreground">
              <div className="text-center">
                <Pencil className="h-8 w-8 mx-auto mb-2 opacity-50" />
                <p>Select a skill to edit, or add a new one.</p>
              </div>
            </div>
          )}
        </div>

        <DialogFooter className="px-6 py-3 border-t shrink-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={disabled}>
            <Save className="mr-2 h-4 w-4" />
            Apply Changes
          </Button>
        </DialogFooter>

        {/* Add Skill Dialog */}
        <Dialog open={showAddSkill} onOpenChange={setShowAddSkill}>
          <DialogContent className="sm:max-w-md">
            <DialogHeader>
              <DialogTitle>Add Skill</DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <div>
                <Label className="text-sm">Name</Label>
                <Input
                  value={newSkillName}
                  onChange={(e) =>
                    setNewSkillName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))
                  }
                  placeholder="incident-triage"
                  className="mt-1 font-mono"
                />
              </div>
              <div>
                <Label className="text-sm">Description</Label>
                <Input
                  value={newSkillDesc}
                  onChange={(e) => setNewSkillDesc(e.target.value)}
                  placeholder="Triage incidents step by step"
                  className="mt-1"
                />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setShowAddSkill(false)}>
                Cancel
              </Button>
              <Button onClick={handleAddSkill} disabled={!newSkillName.trim()}>
                <Plus className="mr-2 h-4 w-4" />
                Add
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Add File Dialog */}
        <Dialog open={showAddFile} onOpenChange={setShowAddFile}>
          <DialogContent className="sm:max-w-md">
            <DialogHeader>
              <DialogTitle>Add File</DialogTitle>
            </DialogHeader>
            <div className="py-2">
              <Input
                value={newFilePath}
                onChange={(e) => setNewFilePath(e.target.value)}
                placeholder="scripts/validate.py"
                className="font-mono"
              />
              <p className="text-xs text-muted-foreground mt-2">
                Relative path (max 3 segments).
              </p>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setShowAddFile(false)}>
                Cancel
              </Button>
              <Button onClick={handleAddFile} disabled={!newFilePath.trim()}>
                <Plus className="mr-2 h-4 w-4" />
                Add
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </DialogContent>
    </Dialog>
  );
}
