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
import { useMemo, useState } from 'react';
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query';
import { Users, Loader2, HelpCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
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
import { GrantedGroupsTable, GroupGrantPicker } from '@/components/GroupGrantPicker';
import {
  getDefinitionPermissionsApiV1SchedulerDefinitionsDefinitionIdPermissionsGetOptions,
  getDefinitionPermissionsApiV1SchedulerDefinitionsDefinitionIdPermissionsGetQueryKey,
  getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGetOptions,
  getGroupApiV1GroupsGroupIdGetOptions,
  schedulerShareJobMutation,
  schedulerAddGroupDefaultJobMutation,
  schedulerRemoveGroupDefaultJobMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { useGroupDefaults } from '@/hooks/use-group-defaults';
import { useGroupGrants, type GrantRole, type GrantTarget } from '@/hooks/use-group-grants';
import { getErrorMessage } from '@/lib/utils';
import { toast } from 'sonner';

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
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* A fixed height, not a max: the group list below changes length as you search
          and page, and a content-sized dialog re-centres and jumps on every keystroke. */}
      <DialogContent className="sm:max-w-3xl h-[85vh] flex flex-col">
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
        {/* Mounted only while open, so unsaved edits and the search reset on close. */}
        <JobPermissionsBody definitionId={definitionId} jobName={jobName} onOpenChange={onOpenChange} />
      </DialogContent>
    </Dialog>
  );
}

function JobPermissionsBody({
  definitionId,
  jobName,
  onOpenChange,
}: Omit<JobPermissionsDialogProps, 'open'>) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);

  // The whole grant set, unpaged: saving replaces it.
  const {
    data: currentPermissions,
    isLoading: isLoadingPermissions,
    error: permissionsError,
  } = useQuery({
    ...getDefinitionPermissionsApiV1SchedulerDefinitionsDefinitionIdPermissionsGetOptions({
      path: { definition_id: definitionId },
    }),
    retry: false,
  });

  const { grants, roleOf, setRole, hasChanges: grantsChanged, toPermissions } =
    useGroupGrants(currentPermissions);

  const grantedGroupIds = useMemo(
    () => currentPermissions?.map((perm) => perm.user_group_id) ?? [],
    [currentPermissions],
  );

  // Which groups have this job as a default. One request per group holding a grant,
  // as the sub-agent dialog does: the group page owns the default list, and there is
  // no "which groups default to this job" read. Each list is read WHOLE — the
  // question is whether one definition id is in it, there is no id filter, and a page
  // of it would miss the job whenever it sorts past that page.
  const {
    defaults,
    added: newDefaults,
    removed: droppedDefaults,
    hasChanges: defaultsChanged,
    setDefault,
    isLoading: isLoadingDefaults,
  } = useGroupDefaults({
    queryKey: ['job-group-defaults', definitionId],
    groupIds: grantedGroupIds,
    isDefaultFor: async (groupId) => {
      const rows = await queryClient.fetchQuery(
        getGroupAccessibleJobsApiV1GroupsGroupIdAccessibleJobsGetOptions({
          path: { group_id: groupId },
        }),
      );
      return rows.some((job) => job.id === definitionId && job.is_default);
    },
    granted: (groupId) => roleOf(groupId) !== undefined,
  });

  const grantFor = (groupId: number) => grants.find((g) => g.groupId === groupId);

  // Newly switched-on defaults, and how many people they turn the job on for. This is
  // the number the confirmation names — the honest cost of the decision, since each of
  // those members gets a run of their own. A group picked from the list brings its
  // count along; one that already held a grant does not (permission rows carry only
  // the name), so its count is read from the group itself, and only once it matters.
  const memberCountQueries = useQueries({
    queries: newDefaults
      .filter((groupId) => grantFor(groupId)?.memberCount === undefined)
      .map((groupId) => ({
        ...getGroupApiV1GroupsGroupIdGetOptions({ path: { group_id: groupId } }),
        retry: false,
      })),
  });
  const memberCountOf = (groupId: number) =>
    grantFor(groupId)?.memberCount ??
    memberCountQueries.find((q) => q.data?.data.id === groupId)?.data?.data.member_count;
  const newDefaultCounts = newDefaults.map(memberCountOf);
  const affectedMembers = newDefaultCounts.every((n) => n !== undefined)
    ? newDefaultCounts.reduce<number>((total, n) => total + (n ?? 0), 0)
    : undefined;

  const hasChanges = grantsChanged || defaultsChanged;

  const changeRole = (group: GrantTarget, role: GrantRole | null) => {
    // A default without a grant behind it is not a state the backend accepts.
    if (role === null) setDefault(group.id, false);
    setRole(group, role);
  };

  const shareMutation = useMutation({ ...schedulerShareJobMutation() });
  const addDefaultMutation = useMutation({ ...schedulerAddGroupDefaultJobMutation() });
  const removeDefaultMutation = useMutation({ ...schedulerRemoveGroupDefaultJobMutation() });

  const save = async () => {
    setSaving(true);
    try {
      // Permissions first, always: a default is only accepted for a group that already
      // has the grant, so the two calls are ordered, not merely both made.
      await shareMutation.mutateAsync({
        path: { definition_id: definitionId },
        body: { group_permissions: toPermissions() },
      });
      await Promise.all([
        ...newDefaults.map((group_id) =>
          addDefaultMutation.mutateAsync({ path: { group_id, definition_id: definitionId } }),
        ),
        ...droppedDefaults.map((group_id) =>
          removeDefaultMutation.mutateAsync({
            path: { group_id, definition_id: definitionId },
          }),
        ),
      ]);
      toast.success('Sharing updated');
      // Both keys: sharing changes `subscriber_count`, and the detail page this dialog
      // was opened from decides its whole sharing surface from it — the badge, "Suspend
      // for all"/"Reset schedules", and what the pause and delete tooltips say. Keyed on
      // the prefix because the dialog knows the DEFINITION id, not the subscription id
      // the detail query is keyed by.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['scheduler-jobs'] }),
        queryClient.invalidateQueries({ queryKey: ['scheduler-job'] }),
        queryClient.invalidateQueries({
          queryKey: getDefinitionPermissionsApiV1SchedulerDefinitionsDefinitionIdPermissionsGetQueryKey({
            path: { definition_id: definitionId },
          }),
        }),
        queryClient.invalidateQueries({ queryKey: ['job-group-defaults', definitionId] }),
      ]);
      onOpenChange(false);
    } catch (err) {
      toast.error('Could not update sharing', { description: getErrorMessage(err) });
    } finally {
      setSaving(false);
      setConfirming(false);
    }
  };

  return (
    <>
      <div className="flex-1 overflow-y-auto min-h-0 space-y-6">
        {isLoadingPermissions ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading sharing…
          </div>
        ) : permissionsError ? (
          <div className="py-8 text-center">
            <p className="text-sm text-destructive mb-2">{getErrorMessage(permissionsError)}</p>
            <p className="text-xs text-muted-foreground">
              You do not have permission to manage sharing for this job.
            </p>
          </div>
        ) : (
          <>
            <GrantedGroupsTable
              grants={grants}
              onRoleChange={changeRole}
              roleHelp={
                <>
                  <strong>Read:</strong> members may activate or copy the job
                  <br />
                  <strong>Write:</strong> members may also edit it, suspend it and share it on
                </>
              }
              extraColumn={{
                className: 'w-[160px]',
                header: (
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
                ),
                cell: (grant) => (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <div className="inline-flex">
                          <Checkbox
                            checked={defaults.has(grant.groupId)}
                            disabled={isLoadingDefaults}
                            onCheckedChange={(checked) => setDefault(grant.groupId, checked === true)}
                          />
                        </div>
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">
                          {grant.memberCount === undefined
                            ? 'Activates the job for every member of the group.'
                            : grant.memberCount === 1
                              ? 'Activates the job for the 1 member.'
                              : `Activates the job for all ${grant.memberCount} members.`}
                        </p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                ),
              }}
              emptyMessage="Not shared with any group yet."
            />
            {/* The groups the user can actually share to — their own. An administrator
                sharing somebody else's job shares it to their own groups too: the grant
                is theirs to make. */}
            <GroupGrantPicker
              roleOf={roleOf}
              onGrant={changeRole}
              emptyMessage="You are not a member of any group, so there is nobody to share this job with."
            />
          </>
        )}
      </div>

      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
        <Button
          onClick={() => (newDefaults.length > 0 ? setConfirming(true) : save())}
          disabled={!hasChanges || saving || !!permissionsError}
        >
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </DialogFooter>

      {/* Switching a job on for other people is confirmed with the count. */}
      <AlertDialog open={confirming} onOpenChange={(o) => !o && setConfirming(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Activate this job for everyone in the group?</AlertDialogTitle>
            <AlertDialogDescription>
              "{jobName}" will start running for{' '}
              {affectedMembers === undefined
                ? 'every member'
                : affectedMembers === 1
                  ? 'the 1 member'
                  : `all ${affectedMembers} members`}{' '}
              of{' '}
              {newDefaults.map((id) => grantFor(id)?.name ?? `group ${id}`).join(', ')}
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
