/**
 * Share a scheduled job with groups (ADR-0010).
 *
 * Two layers, exactly as sub-agents have them and deliberately in the same dialog so
 * they are read as one decision:
 *
 * - a **permission** (`read` / `write`) says who MAY activate the job — read lets a
 *   member subscribe or copy it, write additionally lets them edit what it does;
 * - a **group default** activates it for every current and future member of the group
 *   straight away, under each member's own identity.
 *
 * The second is a grant on other people's behalf — it starts jobs running under their
 * accounts and spends their budget — so it is confirmed with the number of people it
 * affects rather than applied on the checkbox alone.
 *
 * What it shares is the job's DEFINITION. The caller passes the subscription's
 * `definition_id`, which for an unshared job is also the id its owner has always seen.
 */
import { useState, useEffect, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Users, Loader2, Search, HelpCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
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
  consoleListMyGroupsOptions,
  getDefinitionPermissionsApiV1SchedulerDefinitionsDefinitionIdPermissionsGetOptions,
  getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGetOptions,
  schedulerShareJobMutation,
  schedulerAddGroupDefaultJobMutation,
  schedulerRemoveGroupDefaultJobMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { getErrorMessage } from '@/lib/utils';
import type { JobGroupPermission } from '@/api/generated/types.gen';
import { toast } from 'sonner';

type Role = 'none' | 'read' | 'write';

interface JobPermissionsDialogProps {
  /** The job's DEFINITION id — what is shared. Not the subscription id. */
  definitionId: number;
  jobName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function JobPermissionsDialog({
  definitionId,
  jobName,
  open,
  onOpenChange,
}: JobPermissionsDialogProps) {
  const queryClient = useQueryClient();
  const [roles, setRoles] = useState<Map<number, Role>>(new Map());
  const [initialRoles, setInitialRoles] = useState<Map<number, Role>>(new Map());
  const [defaults, setDefaults] = useState<Set<number>>(new Set());
  const [initialDefaults, setInitialDefaults] = useState<Set<number>>(new Set());
  const [searchQuery, setSearchQuery] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);

  const {
    data: currentPermissions,
    isLoading: isLoadingPermissions,
    error: permissionsError,
  } = useQuery({
    ...getDefinitionPermissionsApiV1SchedulerDefinitionsDefinitionIdPermissionsGetOptions({
      path: { definition_id: definitionId },
    }),
    enabled: open,
    retry: false,
  });

  // The groups the user can actually share to — their own, with member counts and
  // without the member list (`console_list_my_groups`). An administrator sharing
  // somebody else's job shares it to their own groups too: the grant is theirs to make.
  const {
    data: groups,
    isLoading: isLoadingGroups,
    error: groupsError,
  } = useQuery({ ...consoleListMyGroupsOptions(), enabled: open, retry: false });

  const availableGroups = useMemo(() => groups ?? [], [groups]);

  useEffect(() => {
    if (!currentPermissions) return;
    const next = new Map<number, Role>();
    currentPermissions.forEach((perm) => {
      const role: Role = perm.permissions.includes('write')
        ? 'write'
        : perm.permissions.includes('read')
          ? 'read'
          : 'none';
      if (role !== 'none') next.set(perm.user_group_id, role);
    });
    setRoles(next);
    setInitialRoles(new Map(next));
  }, [currentPermissions]);

  // Which of those groups have this job as a default. One request per group, as the
  // sub-agent dialog does: the group page owns the default list, and there is no
  // "which groups default to this job" read.
  useEffect(() => {
    if (!open || availableGroups.length === 0) return;
    let cancelled = false;
    (async () => {
      const found = new Set<number>();
      await Promise.all(
        availableGroups.map(async (group) => {
          try {
            const rows = await queryClient.fetchQuery(
              getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGetOptions({
                path: { group_id: group.id },
              }),
            );
            if (rows?.some((job) => job.id === definitionId && job.is_default)) {
              found.add(group.id);
            }
          } catch {
            // One unreadable group must not blank the others out.
          }
        }),
      );
      if (cancelled) return;
      setDefaults(found);
      setInitialDefaults(new Set(found));
    })();
    return () => {
      cancelled = true;
    };
  }, [open, availableGroups, definitionId, queryClient]);

  useEffect(() => {
    if (open) return;
    setSearchQuery('');
    setDefaults(new Set());
    setInitialDefaults(new Set());
    setConfirming(false);
  }, [open]);

  const shareMutation = useMutation({ ...schedulerShareJobMutation() });
  const addDefaultMutation = useMutation({ ...schedulerAddGroupDefaultJobMutation() });
  const removeDefaultMutation = useMutation({ ...schedulerRemoveGroupDefaultJobMutation() });

  const sameSet = (a: Set<number>, b: Set<number>) =>
    a.size === b.size && [...a].every((id) => b.has(id));
  const sameRoles = (a: Map<number, Role>, b: Map<number, Role>) =>
    a.size === b.size && [...a].every(([id, role]) => b.get(id) === role);

  const hasChanges = !sameRoles(roles, initialRoles) || !sameSet(defaults, initialDefaults);

  // Newly switched-on defaults, and how many people they turn the job on for. This is
  // the number the confirmation names — the honest cost of the decision, since each of
  // those members gets a run of their own.
  const newDefaults = [...defaults].filter((id) => !initialDefaults.has(id));
  const affectedMembers = newDefaults.reduce(
    (total, id) => total + (availableGroups.find((g) => g.id === id)?.member_count ?? 0),
    0,
  );

  const setRole = (groupId: number, role: Role) => {
    setRoles((prev) => {
      const next = new Map(prev);
      if (role === 'none') {
        next.delete(groupId);
        // A default without a grant behind it is not a state the backend accepts.
        setDefaults((d) => {
          const copy = new Set(d);
          copy.delete(groupId);
          return copy;
        });
      } else {
        next.set(groupId, role);
      }
      return next;
    });
  };

  const save = async () => {
    setSaving(true);
    try {
      const group_permissions: JobGroupPermission[] = [...roles.entries()].map(
        ([user_group_id, role]) => ({
          user_group_id,
          permissions: role === 'write' ? ['read', 'write'] : ['read'],
        }),
      );
      // Permissions first, always: a default is only accepted for a group that already
      // has the grant, so the two calls are ordered, not merely both made.
      await shareMutation.mutateAsync({
        path: { definition_id: definitionId },
        body: { group_permissions },
      });
      await Promise.all([
        ...newDefaults.map((group_id) =>
          addDefaultMutation.mutateAsync({ path: { group_id, definition_id: definitionId } }),
        ),
        ...[...initialDefaults]
          .filter((id) => !defaults.has(id))
          .map((group_id) =>
            removeDefaultMutation.mutateAsync({
              path: { group_id, definition_id: definitionId },
            }),
          ),
      ]);
      toast.success('Sharing updated');
      await queryClient.invalidateQueries({ queryKey: ['scheduler-jobs'] });
      onOpenChange(false);
    } catch (err) {
      toast.error('Could not update sharing', { description: getErrorMessage(err) });
    } finally {
      setSaving(false);
      setConfirming(false);
    }
  };

  const isLoading = isLoadingPermissions || isLoadingGroups;
  const loadError = permissionsError || groupsError;

  const displayGroups = availableGroups
    .filter((group) => {
      if (!searchQuery) return true;
      const query = searchQuery.toLowerCase();
      return (
        group.name.toLowerCase().includes(query) ||
        (group.description?.toLowerCase().includes(query) ?? false)
      );
    })
    .sort((a, b) => {
      const aShared = roles.has(a.id);
      const bShared = roles.has(b.id);
      if (aShared === bShared) return a.name.localeCompare(b.name);
      return aShared ? -1 : 1;
    });

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-3xl max-h-[85vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Users className="h-5 w-5" />
              Share this job
            </DialogTitle>
            <DialogDescription>
              Who can run "{jobName}". Every member who activates it runs it under their own
              account, with their own credentials — so sharing means one run per person, not
              one run sent to several people.
            </DialogDescription>
          </DialogHeader>

          <div className="flex-1 flex flex-col min-h-0 space-y-4">
            {isLoading && (
              <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading groups…
              </div>
            )}

            {loadError && (
              <div className="py-8 text-center">
                <p className="text-sm text-destructive mb-2">{getErrorMessage(loadError)}</p>
                <p className="text-xs text-muted-foreground">
                  {permissionsError
                    ? 'You do not have permission to manage sharing for this job.'
                    : 'Failed to load your groups.'}
                </p>
              </div>
            )}

            {!isLoading && !loadError && availableGroups.length === 0 && (
              <div className="py-8 text-center text-sm text-muted-foreground">
                You are not a member of any group, so there is nobody to share this job with.
              </div>
            )}

            {!isLoading && !loadError && availableGroups.length > 0 && (
              <>
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search groups…"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="pl-9"
                  />
                </div>

                <div className="border rounded-md flex-1 overflow-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Group</TableHead>
                        <TableHead className="w-[180px]">
                          <div className="flex items-center gap-1">
                            Permission
                            <TooltipProvider>
                              <Tooltip>
                                <TooltipTrigger asChild>
                                  <HelpCircle className="h-3.5 w-3.5 text-muted-foreground cursor-help" />
                                </TooltipTrigger>
                                <TooltipContent>
                                  <p className="max-w-xs">
                                    <strong>Read:</strong> members may activate or copy the job
                                    <br />
                                    <strong>Write:</strong> members may also edit it, suspend it
                                    and share it on
                                  </p>
                                </TooltipContent>
                              </Tooltip>
                            </TooltipProvider>
                          </div>
                        </TableHead>
                        <TableHead className="w-[160px]">
                          <div className="flex items-center gap-1">
                            Activate for all
                            <TooltipProvider>
                              <Tooltip>
                                <TooltipTrigger asChild>
                                  <HelpCircle className="h-3.5 w-3.5 text-muted-foreground cursor-help" />
                                </TooltipTrigger>
                                <TooltipContent>
                                  <p className="max-w-xs">
                                    Turns the job on for every current and future member of the
                                    group, each running it under their own account. They are
                                    told, and can turn it off.
                                  </p>
                                </TooltipContent>
                              </Tooltip>
                            </TooltipProvider>
                          </div>
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {displayGroups.length === 0 ? (
                        <TableRow>
                          <TableCell colSpan={3} className="text-center py-8 text-muted-foreground">
                            No groups match your search.
                          </TableCell>
                        </TableRow>
                      ) : (
                        displayGroups.map((group) => {
                          const role = roles.get(group.id) ?? 'none';
                          return (
                            <TableRow key={group.id}>
                              <TableCell>
                                <div className="font-medium">{group.name}</div>
                                <div className="text-xs text-muted-foreground">
                                  {group.member_count}{' '}
                                  {group.member_count === 1 ? 'member' : 'members'}
                                  {group.description && ` • ${group.description}`}
                                </div>
                              </TableCell>
                              <TableCell>
                                <Select
                                  value={role}
                                  onValueChange={(value) => setRole(group.id, value as Role)}
                                >
                                  <SelectTrigger className="w-[160px]">
                                    <SelectValue />
                                  </SelectTrigger>
                                  <SelectContent>
                                    <SelectItem value="none">No access</SelectItem>
                                    <SelectItem value="read">Read</SelectItem>
                                    <SelectItem value="write">Write</SelectItem>
                                  </SelectContent>
                                </Select>
                              </TableCell>
                              <TableCell>
                                <TooltipProvider>
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <div className="inline-flex">
                                        <Checkbox
                                          checked={defaults.has(group.id)}
                                          disabled={role === 'none'}
                                          onCheckedChange={(checked) => {
                                            setDefaults((prev) => {
                                              const next = new Set(prev);
                                              if (checked) next.add(group.id);
                                              else next.delete(group.id);
                                              return next;
                                            });
                                          }}
                                        />
                                      </div>
                                    </TooltipTrigger>
                                    <TooltipContent>
                                      <p className="max-w-xs">
                                        {role === 'none'
                                          ? 'Give the group access first.'
                                          : `Activates the job for all ${group.member_count} members.`}
                                      </p>
                                    </TooltipContent>
                                  </Tooltip>
                                </TooltipProvider>
                              </TableCell>
                            </TableRow>
                          );
                        })
                      )}
                    </TableBody>
                  </Table>
                </div>
              </>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => (newDefaults.length > 0 ? setConfirming(true) : save())}
              disabled={!hasChanges || saving || !!loadError}
            >
              {saving ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Switching a job on for other people is confirmed with the count. */}
      <AlertDialog open={confirming} onOpenChange={(o) => !o && setConfirming(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Activate this job for everyone in the group?</AlertDialogTitle>
            <AlertDialogDescription>
              "{jobName}" will start running for all {affectedMembers}{' '}
              {affectedMembers === 1 ? 'member' : 'members'} of{' '}
              {newDefaults
                .map((id) => availableGroups.find((g) => g.id === id)?.name ?? `group ${id}`)
                .join(', ')}
              , each under their own account and using their own credentials. They will be told,
              and can turn it off.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>Cancel</AlertDialogCancel>
            <AlertDialogAction disabled={saving} onClick={save}>
              {saving ? 'Saving…' : 'Activate'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
