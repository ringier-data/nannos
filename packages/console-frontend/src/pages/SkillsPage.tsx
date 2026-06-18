import { useState, useCallback } from 'react';
import { useSearchParams } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Blocks,
  Plus,
  Trash2,
  Pencil,
  Users,
  User as UserIcon,
  Bot,
  Lock,
  Loader2,
  File,
  X,
  ChevronDown,
  Download,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  listSkillsApiV1PlaybooksAgentsAgentNameSkillsGetOptions,
  listSkillsApiV1PlaybooksAgentsAgentNameSkillsGetQueryKey,
  createSkillApiV1PlaybooksAgentsAgentNameSkillsScopePostMutation,
  deleteSkillApiV1PlaybooksAgentsAgentNameSkillsScopeSkillNameDeleteMutation,
  consoleListSubAgentsOptions,
  listMyGroupsApiV1GroupsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import { getSkillApiV1PlaybooksAgentsAgentNameSkillsSkillNameGet, deleteSkillApiV1PlaybooksAgentsAgentNameSkillsScopeSkillNameDelete, createSkillApiV1PlaybooksAgentsAgentNameSkillsScopePost, listSkillFilesApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesGet, getSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathGet, writeSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathPut } from '@/api/generated/sdk.gen';
import type { ConsoleBackendModelsSkillsRegistrySkillSummary as SkillSummary, SkillDefinition } from '@/api/generated/types.gen';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
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
import { SkillEditorPanel } from '@/components/skills/SkillEditorPanel';
import { SkillImportPanel } from '@/components/skills/SkillImportPanel';

export function SkillsPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  // Derive state from URL
  const selectedAgent = searchParams.get('agent') || 'orchestrator';
  // Unified view: 'all' | 'personal' | 'group:<id>' | 'standard'
  const view = searchParams.get('view') || 'all';

  // Parse view into scope info for API calls
  const isGroupView = view.startsWith('group:');
  const viewGroupId = isGroupView ? view.slice(6) : undefined;
  const apiScope = isGroupView ? 'group' : 'personal';
  const groupIdParam = viewGroupId;

  // Active skill for the editor panel
  const [activeSkill, setActiveSkill] = useState<{ name: string; scope: string } | null>(null);

  // Import panel state
  const [showImport, setShowImport] = useState(false);

  // Create dialog state
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [newSkillName, setNewSkillName] = useState('');

  // Delete confirmation state
  const [deletingSkill, setDeletingSkill] = useState<SkillSummary | null>(null);

  // Inline rename state
  const [renamingSkill, setRenamingSkill] = useState<SkillSummary | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [isRenaming, setIsRenaming] = useState(false);

  // Scope section collapsed
  const [scopeOpen, setScopeOpen] = useState(true);

  /** Update URL params while preserving others */
  const updateParams = useCallback(
    (updates: Record<string, string>) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(updates)) {
          if (
            (k === 'agent' && v === 'orchestrator') ||
            (k === 'view' && v === 'all')
          ) {
            next.delete(k);
          } else {
            next.set(k, v);
          }
        }
        return next;
      }, { replace: true });
    },
    [setSearchParams],
  );

  const handleAgentChange = (agent: string) => {
    updateParams({ agent });
    setActiveSkill(null);
  };

  // Fetch sub-agents for the agent selector
  const { data: subAgentsData } = useQuery(consoleListSubAgentsOptions());

  // Fetch user's groups
  const { data: myGroupsData } = useQuery(listMyGroupsApiV1GroupsGetOptions());
  const groups = Array.isArray(myGroupsData) ? myGroupsData : [];

  const agentNames = [
    'orchestrator',
    ...(subAgentsData?.items?.map((a) => a.name).filter((name): name is string => Boolean(name) && name !== 'voice-agent') ?? []),
  ];

  // Fetch skills list (aggregates personal + all groups)
  const { data: skillsData, isLoading: skillsLoading } = useQuery({
    ...listSkillsApiV1PlaybooksAgentsAgentNameSkillsGetOptions({
      path: { agent_name: selectedAgent },
    }),
  });

  const allSkills = skillsData?.items ?? [];

  // Derive standard skills from the sub-agent config
  const selectedSubAgent = subAgentsData?.items?.find((a) => a.name === selectedAgent);
  const standardSkills: Array<Omit<SkillDefinition, 'scope'> & { scope: 'standard' }> = (
    selectedSubAgent?.config_version?.skills ?? []
  ).map((s) => ({ ...s, description: s.description ?? '', scope: 'standard' as const }));

  const standardSkillNames = new Set(standardSkills.map((s) => s.name));
  const isOverride = (skill: SkillSummary) => standardSkillNames.has(skill.name);

  // Filter skills based on unified view
  const filteredSkills: Array<(SkillSummary & { scope: string }) | (Omit<SkillDefinition, 'scope'> & { scope: 'standard' })> = (() => {
    switch (true) {
      case view === 'personal':
        return allSkills.filter((s) => s.scope === 'personal');
      case isGroupView: {
        return allSkills.filter((s) => s.scope === 'group' && s.group_id === viewGroupId);
      }
      case view === 'standard':
        return standardSkills;
      default: {
        // "all" — show personal skills merged with standard (personal overrides shown, standard where no override)
        const personalSkills = allSkills.filter((s) => s.scope === 'personal');
        return [...personalSkills, ...standardSkills.filter((std) => !personalSkills.some((s) => s.name === std.name))];
      }
    }
  })();

  // Build tab items: All, Personal, one per group, Standard
  const viewTabs: Array<{ key: string; label: string; icon?: 'user' | 'users' | 'lock' }> = [
    { key: 'all', label: 'All' },
    { key: 'personal', label: 'Personal', icon: 'user' },
    ...groups.map((g) => ({ key: `group:${g.id}`, label: g.name, icon: 'users' as const })),
    { key: 'standard', label: 'Standard', icon: 'lock' },
  ];

  // Mutations
  const invalidateSkills = () =>
    queryClient.invalidateQueries({
      queryKey: listSkillsApiV1PlaybooksAgentsAgentNameSkillsGetQueryKey({
        path: { agent_name: selectedAgent },
      }),
    });

  const createSkillMutation = useMutation({
    ...createSkillApiV1PlaybooksAgentsAgentNameSkillsScopePostMutation(),
    onSuccess: () => {
      toast.success('Skill created');
      invalidateSkills();
      setShowCreateDialog(false);
      // Auto-open the editor for the newly created skill
      setActiveSkill({ name: newSkillName, scope: apiScope });
      setNewSkillName('');
    },
    onError: () => toast.error('Failed to create skill'),
  });

  const deleteSkillMutation = useMutation({
    ...deleteSkillApiV1PlaybooksAgentsAgentNameSkillsScopeSkillNameDeleteMutation(),
    onSuccess: () => {
      toast.success('Skill deleted');
      invalidateSkills();
      setDeletingSkill(null);
      if (activeSkill && deletingSkill && activeSkill.name === deletingSkill.name) {
        setActiveSkill(null);
      }
    },
    onError: () => toast.error('Failed to delete skill'),
  });

  const handleCreateSkill = () => {
    if (!newSkillName.trim()) {
      toast.error('Skill name is required');
      return;
    }
    if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(newSkillName)) {
      toast.error('Name must be lowercase letters, numbers, and hyphens only');
      return;
    }
    createSkillMutation.mutate({
      path: { agent_name: selectedAgent, scope: apiScope },
      query: { group_id: groupIdParam },
      body: { name: newSkillName, description: '', content: '' } as any,
    });
  };

  const handleRenameSkill = async (skill: SkillSummary, newName: string) => {
    if (!newName.trim() || newName === skill.name) {
      setRenamingSkill(null);
      return;
    }
    if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(newName)) {
      toast.error('Name must be lowercase letters, numbers, and hyphens only');
      return;
    }
    setIsRenaming(true);
    try {
      const groupId = skill.scope === 'group' ? (skill as any).group_id : undefined;
      // Fetch existing skill content (SKILL.md)
      const existing = await getSkillApiV1PlaybooksAgentsAgentNameSkillsSkillNameGet({
        path: { agent_name: selectedAgent, skill_name: skill.name },
        query: { scope: skill.scope, group_id: groupId },
        throwOnError: true,
      });
      // Parse the raw SKILL.md into description + body (the create endpoint generates frontmatter from name+description)
      const rawContent = existing.data.content ?? '';
      const fmMatch = rawContent.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
      let description = '';
      let body = '';
      if (fmMatch) {
        const frontmatter = fmMatch[1];
        body = (fmMatch[2] ?? '').trim();
        const descMatch = frontmatter.match(/^description:\s*(.+)$/m);
        if (descMatch) description = descMatch[1].trim();
      } else {
        body = rawContent.trim();
      }
      // Create skill with new name (backend generates frontmatter from name + description)
      await createSkillApiV1PlaybooksAgentsAgentNameSkillsScopePost({
        path: { agent_name: selectedAgent, scope: skill.scope },
        query: { group_id: groupId },
        body: { name: newName, description, content: body },
        throwOnError: true,
      });
      // Copy all additional files from old skill to new skill
      const filesResp = await listSkillFilesApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesGet({
        path: { agent_name: selectedAgent, skill_name: skill.name },
        query: { scope: skill.scope, group_id: groupId },
        throwOnError: true,
      });
      const files = filesResp.data.items ?? [];
      for (const file of files) {
        // Skip SKILL.md since it's already handled by createSkill
        if (file.path === 'SKILL.md') continue;
        const fileContent = await getSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathGet({
          path: { agent_name: selectedAgent, skill_name: skill.name, file_path: file.path },
          query: { scope: skill.scope, group_id: groupId },
          throwOnError: true,
        });
        await writeSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathPut({
          path: { agent_name: selectedAgent, skill_name: newName, file_path: file.path },
          query: { scope: skill.scope, group_id: groupId },
          body: { content: fileContent.data.content ?? '' },
          throwOnError: true,
        });
      }
      // Delete old skill
      await deleteSkillApiV1PlaybooksAgentsAgentNameSkillsScopeSkillNameDelete({
        path: { agent_name: selectedAgent, scope: skill.scope, skill_name: skill.name },
        query: { group_id: groupId },
        throwOnError: true,
      });
      toast.success(`Renamed to "${newName}"`);
      // Remove old skill data from cache and invalidate all skill queries
      queryClient.removeQueries({
        predicate: (query) => {
          const key = query.queryKey[0];
          if (typeof key === 'object' && key !== null) {
            const id = (key as any)._id as string | undefined;
            const path = (key as any).path as any;
            // Remove old skill detail and file queries
            if (id === 'getSkillApiV1PlaybooksAgentsAgentNameSkillsSkillNameGet' && path?.skill_name === skill.name) {
              return true;
            }
          }
          if (Array.isArray(query.queryKey) && query.queryKey[0] === 'skill-files') {
            return true;
          }
          return false;
        },
      });
      await queryClient.invalidateQueries({
        predicate: (query) => {
          const key = query.queryKey[0];
          if (typeof key === 'object' && key !== null) {
            const id = (key as any)._id as string | undefined;
            return id === 'getSkillApiV1PlaybooksAgentsAgentNameSkillsSkillNameGet'
              || id === 'listSkillsApiV1PlaybooksAgentsAgentNameSkillsGet';
          }
          return false;
        },
      });
      invalidateSkills();
      if (activeSkill?.name === skill.name) {
        setActiveSkill({ name: newName, scope: skill.scope });
      }
    } catch {
      toast.error('Failed to rename skill');
    } finally {
      setIsRenaming(false);
      setRenamingSkill(null);
    }
  };

  const handleConfirmDeleteSkill = () => {
    if (!deletingSkill) return;
    deleteSkillMutation.mutate({
      path: {
        agent_name: selectedAgent,
        scope: deletingSkill.scope,
        skill_name: deletingSkill.name,
      },
      query: { group_id: deletingSkill.scope === 'group' ? (deletingSkill as any).group_id : undefined },
    });
  };

  // Label for the create dialog — depends on which view tab is active
  const scopeLabel = view === 'personal' || view === 'all'
    ? 'Personal'
    : isGroupView
    ? groups.find((g) => String(g.id) === viewGroupId)?.name ?? 'Group'
    : 'Personal';

  return (
    <div className="flex h-full">
      {/* Left sidebar — explorer panel */}
      <div className="w-72 shrink-0 border-r flex flex-col bg-muted/20">
        {/* Sidebar header — agent selector */}
        <div className="px-3 py-2 border-b space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
              Skills
            </span>
            <div className="flex items-center gap-0.5">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-6 w-6 p-0"
                    onClick={() => { setShowImport(true); setActiveSkill(null); }}
                  >
                    <Download className="h-3.5 w-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Import from registry</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-6 w-6 p-0"
                    onClick={() => setShowCreateDialog(true)}
                    disabled={view === 'standard'}
                  >
                    <Plus className="h-3.5 w-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>New skill</TooltipContent>
              </Tooltip>
            </div>
          </div>
          <Select value={selectedAgent} onValueChange={handleAgentChange}>
            <SelectTrigger className="h-8 text-xs bg-background w-full">
              <Bot className="h-3 w-3 text-muted-foreground shrink-0 mr-1.5" />
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {agentNames.map((name) => (
                <SelectItem key={name} value={name}>
                  {name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* View tabs — unified scope + filter, collapsible */}
        <div className="flex flex-col border-b">
          <button
            onClick={() => setScopeOpen(!scopeOpen)}
            className="flex items-center justify-between px-3 pt-2 pb-1 hover:bg-accent/30 transition-colors"
          >
            <span className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
              Scope{!scopeOpen && `: ${viewTabs.find((t) => t.key === view)?.label ?? 'All'}`}
            </span>
            <ChevronDown className={`h-3 w-3 text-muted-foreground transition-transform ${scopeOpen ? '' : '-rotate-90'}`} />
          </button>
          {scopeOpen && viewTabs.map((tab) => {
            const isActive = view === tab.key;
            return (
              <button
                key={tab.key}
                onClick={() => {
                  updateParams({ view: tab.key });
                  setActiveSkill(null);
                }}
                className={`flex items-center gap-2 px-3 py-1.5 text-xs transition-colors text-left ${
                  isActive
                    ? 'bg-accent text-accent-foreground font-medium border-l-2 border-primary'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/50 border-l-2 border-transparent'
                }`}
              >
                {tab.icon === 'user' && <UserIcon className="h-3 w-3 shrink-0" />}
                {tab.icon === 'users' && <Users className="h-3 w-3 shrink-0" />}
                {tab.icon === 'lock' && <Lock className="h-3 w-3 shrink-0" />}
                {!tab.icon && <Blocks className="h-3 w-3 shrink-0" />}
                <span className="truncate">{tab.label}</span>
              </button>
            );
          })}
        </div>

        {/* Skills list */}
        <span className="px-3 pt-2 pb-1 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
          Skills
        </span>
        <div className="flex-1 overflow-y-auto">
          {skillsLoading ? (
            <div className="px-3 py-3 space-y-2">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : filteredSkills.length === 0 ? (
            <div className="text-center text-muted-foreground py-8 px-3">
              <Blocks className="mx-auto h-6 w-6 mb-1.5 opacity-50" />
              <p className="text-xs">No skills found</p>
              <p className="text-[11px] mt-0.5">Click + to create one</p>
            </div>
          ) : (
            filteredSkills.map((skill) => {
              const isStandard = skill.scope === 'standard';
              const overridesStandard = !isStandard && isOverride(skill as SkillSummary);
              const isActive = activeSkill?.name === skill.name && activeSkill?.scope === skill.scope;
              const fileCount = 'file_count' in skill ? (skill as any).file_count : undefined;

              return (
                <div
                  key={`${skill.scope}-${skill.name}`}
                  className={`group flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors ${
                    isActive
                      ? 'bg-accent text-accent-foreground'
                      : isStandard
                      ? 'opacity-70 cursor-default'
                      : 'hover:bg-accent/50'
                  }`}
                  onClick={() => {
                    if (isStandard) return;
                    setActiveSkill({ name: skill.name!, scope: (skill as SkillSummary).scope });
                  }}
                >
                  <Blocks className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      {renamingSkill?.name === skill.name && renamingSkill?.scope === skill.scope ? (
                        <Input
                          value={renameValue}
                          onChange={(e) =>
                            setRenameValue(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))
                          }
                          onBlur={() => handleRenameSkill(skill as SkillSummary, renameValue)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              handleRenameSkill(skill as SkillSummary, renameValue);
                            } else if (e.key === 'Escape') {
                              setRenamingSkill(null);
                            }
                          }}
                          className="h-5 font-mono text-xs px-1 py-0"
                          autoFocus
                          disabled={isRenaming}
                          onClick={(e) => e.stopPropagation()}
                        />
                      ) : (
                        <span className="font-mono text-xs font-medium truncate">{skill.name}</span>
                      )}
                      {overridesStandard && (
                        <Badge variant="outline" className="text-[9px] px-1 py-0 border-amber-500 text-amber-600 shrink-0">
                          override
                        </Badge>
                      )}
                      {isStandard && <Lock className="h-2.5 w-2.5 text-muted-foreground shrink-0" />}
                      {fileCount != null && fileCount > 0 && (
                        <span className="text-[10px] text-muted-foreground flex items-center gap-0.5 shrink-0">
                          <File className="h-2.5 w-2.5" />
                          {fileCount}
                        </span>
                      )}
                    </div>
                    {skill.description && (
                      <p className="text-[11px] text-muted-foreground truncate">{skill.description}</p>
                    )}
                  </div>
                  {!isStandard && (
                    <>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 w-6 p-0 opacity-0 group-hover:opacity-100 shrink-0"
                        onClick={(e) => {
                          e.stopPropagation();
                          setRenamingSkill(skill as SkillSummary);
                          setRenameValue(skill.name ?? '');
                        }}
                        disabled={isRenaming}
                      >
                        <Pencil className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 w-6 p-0 opacity-0 group-hover:opacity-100 text-destructive hover:text-destructive shrink-0"
                        onClick={(e) => {
                          e.stopPropagation();
                          setDeletingSkill(skill as SkillSummary);
                        }}
                      >
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Editor area */}
      {showImport ? (
        <SkillImportPanel
          onClose={() => setShowImport(false)}
          onImported={(skillName) => {
            setShowImport(false);
            setActiveSkill({ name: skillName, scope: 'default' });
          }}
        />
      ) : activeSkill ? (
        <div className="flex-1 min-w-0 flex flex-col">
          {/* Editor tab bar */}
          <div className="flex items-center justify-between px-4 py-2 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Blocks className="h-4 w-4 text-muted-foreground" />
              <span className="font-mono font-medium text-sm">{activeSkill.name}</span>
              <Badge
                variant={activeSkill.scope === 'personal' ? 'secondary' : 'outline'}
                className="text-[10px] px-1.5 py-0"
              >
                {activeSkill.scope}
              </Badge>
            </div>
            <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={() => setActiveSkill(null)}>
              <X className="h-4 w-4" />
            </Button>
          </div>
          <div className="flex-1 min-h-0">
              <SkillEditorPanel
                key={`${activeSkill.scope}:${activeSkill.name}`}
                agentName={selectedAgent}
                skillName={activeSkill.name}
                scope={activeSkill.scope}
                groupId={activeSkill.scope === 'group' ? groupIdParam : undefined}
              />
          </div>
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center text-muted-foreground">
          <div className="text-center">
            <Blocks className="h-10 w-10 mx-auto mb-3 opacity-30" />
            <p className="text-sm font-medium">Select a skill to edit</p>
            <p className="text-xs mt-1">Or create a new one with the + button</p>
          </div>
        </div>
      )}

      {/* Create Skill Dialog — name only, then opens editor */}
      <Dialog open={showCreateDialog} onOpenChange={setShowCreateDialog}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>
              New {scopeLabel} Skill
              <span className="text-muted-foreground font-normal text-sm ml-2">
                for <code className="bg-muted px-1 rounded text-xs">{selectedAgent}</code>
              </span>
            </DialogTitle>
          </DialogHeader>
          <div className="py-2">
            <Label className="text-sm">Name (identifier)</Label>
            <Input
              value={newSkillName}
              onChange={(e) => setNewSkillName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
              placeholder="incident-triage"
              className="mt-1 font-mono"
              maxLength={64}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleCreateSkill();
              }}
            />
            <p className="text-xs text-muted-foreground mt-1">
              Lowercase letters, numbers, and hyphens only
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowCreateDialog(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreateSkill} disabled={createSkillMutation.isPending}>
              {createSkillMutation.isPending ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Plus className="mr-2 h-4 w-4" />
              )}
              Create &amp; Edit
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation */}
      <AlertDialog open={!!deletingSkill} onOpenChange={(open) => !open && setDeletingSkill(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete skill &quot;{deletingSkill?.name}&quot;?</AlertDialogTitle>
            <AlertDialogDescription>
              {deletingSkill?.scope === 'group'
                ? 'This is a group skill — deleting it affects all group members. This cannot be undone.'
                : 'This action cannot be undone.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmDeleteSkill}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteSkillMutation.isPending ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Trash2 className="mr-2 h-4 w-4" />
              )}
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
