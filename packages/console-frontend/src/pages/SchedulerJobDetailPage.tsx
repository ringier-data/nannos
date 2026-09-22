import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft,
  Pause,
  Play,
  Trash2,
  AlertCircle,
  CheckCircle2,
  Clock,
  Loader2,
  XCircle,
  ExternalLink,
  Save,
  Sparkles,
  Send,
  Undo2,
  Pencil,
  KeyRound,
  Users,
  Ban,
  RotateCcw,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { SubAgentSelect } from '@/components/SubAgentSelect';
import { WatchFields, type WatchFieldsValue } from '@/components/WatchFields';
import { LastCheckPanel } from '@/components/LastCheckPanel';
import { argsModeFor, resolveArgs } from '@/lib/watchArgs';
import { agentActionError, automatedSubAgentParameters } from '@/lib/agentAction';
import { config } from '@/config';
import {
  type JobRunStatus,
  type ScheduledJob,
  type ScheduledJobRun,
  getDeliveryChannels,
  generateJobDraft,
  formatApiError,
  updateScheduledJob,
  runJobNow,
  type DeliveryChannel,
  getJob,
  listRuns,
  pauseJob,
  resumeJob,
  resumeParkedRun,
  deleteJob,
} from '@/api/scheduler';
import {
  consoleListSubAgentsOptions,
  consoleListMcpToolsOptions,
  schedulerSuspendJobMutation,
  schedulerUnsuspendJobMutation,
  schedulerFollowDefaultScheduleMutation,
  schedulerResetJobSchedulesMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { JobPermissionsDialog } from '@/components/scheduler/JobPermissionsDialog';
import { SharingBadge } from '@/components/scheduler/sharing';
import { isOwnJob, subscriberCount } from '@/lib/sharedJobs';
import { useAuth } from '@/contexts/AuthContext';
import { CronField } from '@/components/CronField';
import { describeCron } from '@/lib/cron';
import { DetailSkeleton } from '@/components/skeletons';
import { io } from 'socket.io-client';
import { toast } from 'sonner';

interface SchedulerNotification {
  job_id: number;
  job_name: string;
  run_id: number;
  status: JobRunStatus;
  result_summary?: string;
  error_message?: string;
  // Carried because the badge cannot be derived from the status alone: a run keeps
  // `auth_required` after it is answered, so "still waiting" is the task id. Without it
  // a run-now that parks renders in the past tense until the polled table corrects it.
  parked_task_id?: string | null;
  timestamp: string;
}

interface RunNowResult {
  status: JobRunStatus;
  result_summary?: string | null;
  error_message?: string | null;
  delivered?: boolean | null;
  parked_task_id?: string | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString();
}

function formatDuration(start: string | null | undefined, end: string | null | undefined): string {
  if (!start || !end) return '—';
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}

/**
 * The effective trigger, as a string that changes whenever the server's does.
 *
 * Used as the edit form's `key`: the form seeds its schedule fields from the job once,
 * so a trigger that moves underneath it (the viewer dropping their own schedule, or a
 * writer resetting everyone's) has to remount the form rather than leave it holding —
 * and then resaving — the values that were just cleared.
 */
function triggerIdentity(job: ScheduledJob): string {
  return [
    job.schedule_kind,
    job.cron_expr,
    job.interval_seconds,
    job.run_at,
    job.timezone,
    job.trigger_inherited,
  ].join('|');
}

/** The job's DEFAULT schedule in words — what an inherited subscription follows. */
function defaultScheduleLabel(job: ScheduledJob): string {
  const t = job.trigger_defaults;
  if (!t) return '\u2014';
  if (t.schedule_kind === 'cron') return t.cron_expr ?? '\u2014';
  if (t.schedule_kind === 'interval')
    return t.interval_seconds ? `every ${t.interval_seconds}s` : '\u2014';
  if (t.schedule_kind === 'once' && t.run_at) return new Date(t.run_at).toLocaleString();
  return '\u2014';
}

function scheduleLabel(job: ScheduledJob): string {
  if (job.schedule_kind === 'cron') return job.cron_expr ?? '—';
  if (job.schedule_kind === 'interval') return job.interval_seconds ? `every ${job.interval_seconds}s` : '—';
  if (job.schedule_kind === 'once' && job.run_at) return new Date(job.run_at).toLocaleString();
  return '—';
}

/** Format an instant as YYYY-MM-DDTHH:mm wall-clock in the given IANA timezone
 * (browser-local when omitted or unresolvable). The backend interprets the naive
 * string the form submits in the job's timezone, so prefilling in any other zone
 * would silently shift the run instant on save. */
function toDatetimeLocal(iso: string | Date, timeZone?: string | null): string {
  const d = new Date(iso);
  if (timeZone) {
    try {
      const parts = new Intl.DateTimeFormat('en-CA', {
        timeZone,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
      })
        .formatToParts(d)
        .reduce<Record<string, string>>((acc, p) => {
          acc[p.type] = p.value;
          return acc;
        }, {});
      return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
    } catch {
      // Unresolvable zone name — fall through to browser-local.
    }
  }
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

/** Date-time string in YYYY-MM-DDTHH:mm format suitable for datetime-local input, clamped to "now". */
function nowDatetimeLocal(timeZone?: string | null): string {
  const d = new Date();
  d.setSeconds(0, 0);
  return toDatetimeLocal(d, timeZone);
}

// ---------------------------------------------------------------------------
// Run status badge
// ---------------------------------------------------------------------------

function RunStatusBadge({ run }: { run: Pick<ScheduledJobRun, 'status' | 'parked_task_id'> }) {
  const status = run.status;
  switch (status) {
    case 'success':
      return (
        <Badge className="gap-1 bg-green-600 hover:bg-green-600">
          <CheckCircle2 className="h-3 w-3" /> Success
        </Badge>
      );
    case 'failed':
      return (
        <Badge variant="destructive" className="gap-1">
          <XCircle className="h-3 w-3" /> Failed
        </Badge>
      );
    case 'running':
      return (
        <Badge variant="secondary" className="gap-1">
          <Loader2 className="h-3 w-3 animate-spin" /> Running
        </Badge>
      );
    case 'condition_not_met':
      return (
        <Badge variant="secondary" className="gap-1 text-muted-foreground">
          <AlertCircle className="h-3 w-3" /> Condition not met
        </Badge>
      );
    case 'interrupted':
      // The process running it died; it does not count against the job.
      return (
        <Badge variant="outline" className="gap-1 text-muted-foreground">
          <AlertCircle className="h-3 w-3" /> Interrupted
        </Badge>
      );
    case 'auth_required':
      // Neither a success nor a failure: a tool needed the owner's credential and the
      // run stopped to ask. Showing it as either is how this gap stayed invisible.
      //
      // Present tense only while it is actually still waiting. The STATUS stays
      // `auth_required` for good — it is a true record of how that occurrence ended —
      // so a run that has been answered would otherwise keep claiming to want something,
      // and a chain of them (one authorization leading to the next) reads as several
      // open questions when only the last one is live.
      return run.parked_task_id ? (
        <Badge variant="outline" className="gap-1 border-amber-500 text-amber-600">
          <KeyRound className="h-3 w-3" /> Waiting for authorization
        </Badge>
      ) : (
        <Badge variant="outline" className="gap-1 text-muted-foreground">
          <KeyRound className="h-3 w-3" /> Stopped for authorization
        </Badge>
      );
    default:
      // A status this build does not know yet must still show *something*.
      return (
        <Badge variant="outline" className="gap-1">
          {status}
        </Badge>
      );
  }
}

// ---------------------------------------------------------------------------
// Detail header
// ---------------------------------------------------------------------------

function JobHeader({
  job,
  onPause,
  onResume,
  onDelete,
  onRunNow,
  isPendingPause,
  isPendingResume,
  isPendingDelete,
  isRunningNow,
}: {
  job: ScheduledJob;
  onPause: () => void;
  onResume: () => void;
  onDelete: () => void;
  onRunNow: () => void;
  isPendingPause: boolean;
  isPendingResume: boolean;
  isPendingDelete: boolean;
  isRunningNow: boolean;
}) {
  const qc = useQueryClient();
  const [sharing, setSharing] = useState(false);
  const [resetting, setResetting] = useState(false);
  const canWrite = job.effective_permission !== 'read';
  const others = subscriberCount(job) > 1;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['scheduler-job', job.id] });
    qc.invalidateQueries({ queryKey: ['scheduler-jobs'] });
  };
  const onSharingError = (err: unknown) =>
    toast.error('That did not work', { description: formatApiError(err) });

  const suspend = useMutation({
    ...schedulerSuspendJobMutation(),
    onSuccess: () => {
      toast.success('Suspended for every subscriber');
      invalidate();
    },
    onError: onSharingError,
  });
  const unsuspend = useMutation({
    ...schedulerUnsuspendJobMutation(),
    onSuccess: () => {
      toast.success('Running again');
      invalidate();
    },
    onError: onSharingError,
  });
  const resetSchedules = useMutation({
    ...schedulerResetJobSchedulesMutation(),
    onSuccess: (result) => {
      toast.success(
        result.reset === 0
          ? 'Nobody had their own schedule'
          : `${result.reset} subscriber${result.reset === 1 ? '' : 's'} back on the default`,
      );
      invalidate();
    },
    onError: onSharingError,
  });
  const definitionAction = { path: { definition_id: job.definition_id } };

  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">{job.name}</h1>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <Badge variant="outline" className="capitalize">
            {job.job_type}
          </Badge>
          <Badge variant="outline" className="capitalize">
            {job.schedule_kind}
          </Badge>
          <span className="font-mono">{scheduleLabel(job)}</span>
          {job.schedule_kind === 'cron' && job.timezone && <span className="text-xs">({job.timezone})</span>}
          <SharingBadge job={job} />
          <span>·</span>
          {/* Suspension outranks the viewer's own state: it stops every subscriber, and
              each of them keeps their own on/off choice for when it is lifted. */}
          {job.suspended_at ? (
            <span className="flex items-center gap-1 text-muted-foreground">
              <Ban className="h-3.5 w-3.5" /> Suspended for everyone
              {job.suspended_reason && <span className="text-xs">({job.suspended_reason})</span>}
            </span>
          ) : job.enabled ? (
            <span className="flex items-center gap-1 text-green-600">
              <CheckCircle2 className="h-3.5 w-3.5" /> Active
            </span>
          ) : (
            <span className="flex items-center gap-1 text-muted-foreground">
              <Pause className="h-3.5 w-3.5" /> Paused
              {job.paused_reason && <span className="text-xs">({job.paused_reason})</span>}
            </span>
          )}
        </div>
        {/* Why this person has this job at all, in words rather than a hover. Only for a
            job they did not author — for their own there is nothing to explain. */}
        {!isOwnJob(job) && (
          <p className="mt-1 text-sm text-muted-foreground">
            {job.activated_by === 'group'
              ? `Activated for you because this job is a default of one of your groups. Shared by ${job.owner_email ?? 'another user'}.`
              : `Shared with you by ${job.owner_email ?? 'another user'}.`}{' '}
            It runs under your account, with your credentials.
            {!canWrite && ' You can change your own schedule and delivery; the rest is theirs.'}
          </p>
        )}
      </div>

      <div className="flex gap-2">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="default" size="sm" disabled={isRunningNow} onClick={onRunNow}>
              {isRunningNow ? (
                <>
                  <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
                  Running…
                </>
              ) : (
                <>
                  <Play className="mr-1.5 h-4 w-4" />
                  Run now
                </>
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            Trigger a full test run right now — resolves token, calls agent-runner, delivers webhook
          </TooltipContent>
        </Tooltip>
        {job.enabled ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="outline" size="sm" disabled={isPendingPause} onClick={onPause}>
                <Pause className="mr-1.5 h-4 w-4" />
                Pause
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {others ? 'Stops the job for you only' : 'Stops the job until you resume it'}
            </TooltipContent>
          </Tooltip>
        ) : (
          <Button variant="outline" size="sm" disabled={isPendingResume} onClick={onResume}>
            <Play className="mr-1.5 h-4 w-4" />
            Resume
          </Button>
        )}

        {/* Sharing. The whole group is absent for a job the viewer only runs: sharing it
            on, suspending it and resetting other people's schedules are all writer
            actions on the definition. */}
        {canWrite && (
          <>
            <Button variant="outline" size="sm" onClick={() => setSharing(true)}>
              <Users className="mr-1.5 h-4 w-4" />
              Share
            </Button>
            {job.suspended_at ? (
              <Button
                variant="outline"
                size="sm"
                disabled={unsuspend.isPending}
                onClick={() => unsuspend.mutate(definitionAction)}
              >
                <Play className="mr-1.5 h-4 w-4" />
                Resume for everyone
              </Button>
            ) : (
              others && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={suspend.isPending}
                      onClick={() => suspend.mutate({ ...definitionAction, body: { reason: null } })}
                    >
                      <Ban className="mr-1.5 h-4 w-4" />
                      Suspend for all
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>
                    Stops the job for all {subscriberCount(job)} subscribers. Each keeps their own
                    on/off choice for when you resume it.
                  </TooltipContent>
                </Tooltip>
              )
            )}
            {others && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="outline" size="sm" onClick={() => setResetting(true)}>
                    <Undo2 className="mr-1.5 h-4 w-4" />
                    Reset schedules
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  Puts every subscriber back on the job's default schedule
                </TooltipContent>
              </Tooltip>
            )}
          </>
        )}

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="destructive"
              size="sm"
              className="ml-2"
              disabled={isPendingDelete}
              onClick={onDelete}
            >
              <Trash2 className="mr-1.5 h-4 w-4" />
              {isOwnJob(job) ? 'Delete' : 'Stop running this'}
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            {isOwnJob(job)
              ? others
                ? `Deletes the job for you and ${subscriberCount(job) - 1} other subscriber${subscriberCount(job) > 2 ? 's' : ''}`
                : 'Deletes the job'
              : 'Removes only your own activation'}
          </TooltipContent>
        </Tooltip>
      </div>

      <JobPermissionsDialog
        definitionId={job.definition_id}
        jobName={job.name}
        open={sharing}
        onOpenChange={setSharing}
      />

      {/* Clearing other people's customisation is somebody else's schedule changing
          without them asking, so it is confirmed rather than done on a click. */}
      <AlertDialog open={resetting} onOpenChange={(o) => !o && setResetting(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Put everyone back on the default schedule?</AlertDialogTitle>
            <AlertDialogDescription>
              Every subscriber who set their own schedule for <strong>{job.name}</strong> will
              follow the job's default ({defaultScheduleLabel(job)}) again, including later
              changes to it. They are told. Your own schedule is reset too.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={resetSchedules.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={resetSchedules.isPending}
              onClick={() => {
                setResetting(false);
                resetSchedules.mutate(definitionAction);
              }}
            >
              {resetSchedules.isPending ? 'Resetting…' : 'Reset'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Edit form
// ---------------------------------------------------------------------------

/**
 * The watch fields as one value, so this page can render the same component the create
 * dialog does. Reading them back out of a stored job means deciding two things the job
 * does not state outright: whether the condition is a rule or a judgement, and whether the
 * outcome is a notification or an agent run.
 */
function watchValueFromJob(job: ScheduledJob): WatchFieldsValue {
  return {
    check_tool: job.check_tool ?? '',
    check_args: (job.check_args ?? {}) as Record<string, unknown>,
    check_args_text: job.check_args ? JSON.stringify(job.check_args, null, 2) : '',
    args_mode: 'fields',
    check_args_exprs: (job.check_args_exprs ?? {}) as Record<string, string>,
    cel_expr: job.cel_expr ?? '',
    llm_condition: job.llm_condition ?? '',
    destroy_after_trigger: job.destroy_after_trigger ?? true,
    // A sub-agent is what makes the outcome an agent run; its reply replaces the message.
    outcome: job.sub_agent_id != null ? 'agent' : 'notify',
    notification_message: job.notification_message ?? '',
    sub_agent_mode: 'existing',
    sub_agent_id: job.sub_agent_id != null ? String(job.sub_agent_id) : '',
    prompt: job.prompt ?? '',
    // Only relevant while defining an agent inline, which an existing job never is.
    automated_name: '',
    automated_description: '',
    automated_model: 'tier:standard',
    automated_system_prompt: '',
    automated_mcp_tools: [],
    automated_enable_thinking: false,
    automated_thinking_level: 'low',
  };
}


function EditForm({ job }: { job: ScheduledJob }) {
  const qc = useQueryClient();

  // ── Per-field state ───────────────────────────────────────────────────────
  const [name, setName] = useState(job.name ?? '');
  const [maxFailures, setMaxFailures] = useState(job.max_failures ?? 3);
  const [cronExpr, setCronExpr] = useState(job.cron_expr ?? '');
  const [intervalSeconds, setIntervalSeconds] = useState(
    job.interval_seconds != null ? String(job.interval_seconds) : ''
  );
  const initialRunAt = job.run_at ? toDatetimeLocal(job.run_at, job.timezone) : '';
  const [runAt, setRunAt] = useState(initialRunAt);
  // Task jobs only: a watch's message lives in the watch value, which owns the exclusive
  // choice between notifying and running an agent.
  const [taskPrompt, setTaskPrompt] = useState(job.prompt ?? '');
  const initialSubAgentId = job.sub_agent_id != null ? String(job.sub_agent_id) : '';
  const [subAgentId, setSubAgentId] = useState(initialSubAgentId);
  const [deliveryChannel, setDeliveryChannel] = useState(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (job as any).delivery_channel_id != null ? String((job as any).delivery_channel_id) : ''
  );
  const [voiceCall, setVoiceCall] = useState(job.voice_call ?? false);
  const [dirty, setDirty] = useState(false);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [watch, setWatch] = useState<WatchFieldsValue>(() => watchValueFromJob(job));
  const [aiQuery, setAiQuery] = useState('');
  const [aiLoading, setAiLoading] = useState(false);

  // ── What the viewer is allowed to change (ADR-0010) ──────────────────────
  // The job view is the SUBSCRIPTION with the definition folded in, and
  // `effective_permission` is the viewer's standing on the definition half. A reader
  // owns their subscription — their delivery target, their on/off state — and nothing
  // else; a writer owns what the job does.
  const canWrite = job.effective_permission !== 'read';
  const others = subscriberCount(job) > 1;
  // Whether "is this the job's schedule or mine?" is a question with two answers. It is
  // whenever somebody else owns the default (they can move it under you) OR somebody
  // else follows it (you can move away from them) — NOT only when a second subscriber
  // exists: an owner may unsubscribe and keep the definition, which leaves a lone
  // subscriber whose trigger still diverges from a default they do not control.
  const sharedTrigger = !isOwnJob(job) || others;
  const triggerFixed = job.trigger_policy === 'fixed';
  // FIXED means the owner pins the tick — a watch's tick is part of what the watch
  // means — so only a writer, changing it for everyone, may touch it.
  const canEditTrigger = canWrite || !triggerFixed;
  // The one extra choice a shared job adds to this page, and only once there is somebody
  // else to be ambiguous about. Defaults to the narrowest effect.
  const [scope, setScope] = useState<'mine' | 'everyone'>('mine');
  const showScope = others && canWrite && !triggerFixed;

  // ── Data queries ──────────────────────────────────────────────────────────
  const { data: subAgentsData } = useQuery(
    // Same as the create dialog: a job may run any sub-agent shared with the user.
    consoleListSubAgentsOptions({})
  );
  const subAgents = subAgentsData?.items ?? [];

  const { data: mcpToolsData } = useQuery(consoleListMcpToolsOptions());
  const mcpTools = mcpToolsData?.tools ?? [];

  const { data: channels = [] } = useQuery<DeliveryChannel[]>({
    queryKey: ['delivery-channels'],
    queryFn: getDeliveryChannels,
    staleTime: 60_000,
  });

  // No pre-selection here, unlike the create dialog: on a saved job an empty
  // delivery channel is a real, chosen value ("in-app only"), not a missing
  // default. Filling it in locally would show a channel the job does not have
  // and write it on the next unrelated save.


  function touch() {
    setDirty(true);
    setError(null);
  }

  function resetForm() {
    setName(job.name ?? '');
    setMaxFailures(job.max_failures ?? 3);
    setCronExpr(job.cron_expr ?? '');
    setIntervalSeconds(job.interval_seconds != null ? String(job.interval_seconds) : '');
    setRunAt(initialRunAt);
    setTaskPrompt(job.prompt ?? '');
    setSubAgentId(initialSubAgentId);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    setDeliveryChannel((job as any).delivery_channel_id != null ? String((job as any).delivery_channel_id) : '');
    setVoiceCall(job.voice_call ?? false);
    setWatch(watchValueFromJob(job));
    setDirty(false);
    setError(null);
  }

  // ── AI generation (watch jobs only) ──────────────────────────────────────
  async function handleAiGenerate() {
    if (!aiQuery.trim()) return;
    setAiLoading(true);
    setError(null);
    try {
      const draft = await generateJobDraft(aiQuery);
      setWatch((w) => {
        const next = { ...w };
        if (draft.check_tool) next.check_tool = draft.check_tool;
        if (draft.check_args) {
          const args = draft.check_args as Record<string, unknown>;
          next.check_args = args;
          next.check_args_text = JSON.stringify(args, null, 2);
          next.args_mode = argsModeFor(
            args,
            mcpTools.find((t) => t.name === (draft.check_tool ?? next.check_tool)),
          );
        }
        if (draft.check_args_exprs && Object.keys(draft.check_args_exprs).length > 0) {
          next.check_args_exprs = draft.check_args_exprs as Record<string, string>;
        }
        if (draft.cel_expr) next.cel_expr = draft.cel_expr;
        if (draft.llm_condition) next.llm_condition = draft.llm_condition;
        if (draft.notification_message) {
          next.notification_message = draft.notification_message;
          next.outcome = 'notify';
        }
        if (draft.destroy_after_trigger != null) {
          next.destroy_after_trigger = draft.destroy_after_trigger;
        }
        return next;
      });
      touch();
    } catch (e) {
      setError(`AI generation failed: ${formatApiError(e)}. Please edit the fields manually.`);
    } finally {
      setAiLoading(false);
    }
  }

  // Going back to inheriting is not part of the form: it clears fields rather than
  // setting them, and it lands immediately so the form can reload showing the default.
  const followDefault = useMutation({
    ...schedulerFollowDefaultScheduleMutation(),
    onSuccess: () => {
      toast.success("Following the job's default schedule again");
      qc.invalidateQueries({ queryKey: ['scheduler-job', job.id] });
      qc.invalidateQueries({ queryKey: ['scheduler-jobs'] });
      setDirty(false);
      setEditing(false);
    },
    onError: (err) => {
      toast.error('That did not work', { description: formatApiError(err) });
    },
  });

  // ── Save ──────────────────────────────────────────────────────────────────
  const mutation = useMutation({
    mutationFn: async ({ body, resume }: { body: Record<string, unknown>; resume: boolean }) => {
      await updateScheduledJob(job.id, body);
      if (!resume) return;
      try {
        // After the update, never before: resuming recomputes next_run_at, and it has
        // to compute it from the schedule that was just saved.
        await resumeJob(job.id);
      } catch (e) {
        // The edit is already persisted; only the resume failed — a one-time job that
        // has already run refuses to resume. Reporting this as a failed save would be
        // a lie, and the user would try again on changes that are already stored.
        throw new Error(
          `Changes saved, but the job could not be resumed: ${e instanceof Error ? e.message : String(e)}`
        );
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ['scheduler-job', job.id],
      });
      // Resuming changes the job's status, which the list renders too.
      qc.invalidateQueries({ queryKey: ['scheduler-jobs'] });
      setDirty(false);
      setEditing(false);
    },
    onError: (e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    },
  });

  // Set while a paused job's save waits for the user to say whether to resume it.
  // Holds the built body so the answer costs one click rather than a re-submit.
  const [pendingSave, setPendingSave] = useState<Record<string, unknown> | null>(null);

  function handleSave() {
    if (job.schedule_kind === 'cron' && cronExpr.trim() && !describeCron(cronExpr).ok) {
      setError('Cron expression is invalid');
      return;
    }

    // Arguments are resolved the same way the fields read them, so a JSON editor left
    // mid-edit is reported here rather than being silently dropped on save.
    if (job.job_type === 'watch') {
      const { error: argsError } = resolveArgs(watch);
      if (argsError) {
        setError(argsError);
        return;
      }
      // Both halves are sent as explicit nulls, so emptying both would ask the backend
      // for a watch with nothing to decide with. It refuses; say so here, against the
      // fields the user is looking at.
      if (!watch.cel_expr.trim() && !watch.llm_condition.trim()) {
        setError('Write an expression, a condition for the model to judge, or both.');
        return;
      }
      // The same standard the create dialog holds an agent outcome to. Without it,
      // outcome "agent" with nothing selected saved silently as notify-only — the
      // downgrade this page was already fixed for on the sending side — and an
      // incomplete inline definition surfaced as a raw 422 instead of the message.
      if (watch.outcome === 'agent') {
        const agentError = agentActionError(watch);
        if (agentError) {
          setError(agentError);
          return;
        }
      }
    }

    const body: Record<string, unknown> = {
      name: name || undefined,
      max_failures: maxFailures || undefined,
      ...(job.schedule_kind === 'cron' && { cron_expr: cronExpr || undefined }),
      ...(job.schedule_kind === 'interval' && {
        interval_seconds: intervalSeconds ? parseInt(intervalSeconds) : undefined,
      }),
      // Only send run_at when the user actually changed it: the backend
      // reinterprets any submitted naive value in the job's timezone, so
      // resending the prefill on an unrelated edit would needlessly re-touch
      // the schedule.
      ...(job.schedule_kind === 'once' && runAt !== initialRunAt && { run_at: runAt || undefined }),
      // Only send sub_agent_id when it actually changed: an unchanged value
      // would still re-run the backend's access check on every save, and would
      // reject unrelated edits outright once the agent is no longer accessible.
      // For watches an explicit null clears it back to notify-only; task jobs
      // require one, so an emptied value is simply not sent.
      ...(job.job_type === 'watch'
        ? watch.outcome === 'agent' && watch.sub_agent_mode === 'automated'
          ? // An inline definition is a valid outcome for a watch; it used to be dropped
            // here, so switching to "agent" with one silently PATCHed sub_agent_id: null
            // and left the job notify-only.
            { sub_agent_parameters: automatedSubAgentParameters(watch) }
          : watch.sub_agent_id !== initialSubAgentId && {
              sub_agent_id:
                watch.outcome === 'agent' && watch.sub_agent_id
                  ? parseInt(watch.sub_agent_id)
                  : null,
            }
        : subAgentId !== initialSubAgentId && {
            sub_agent_id: subAgentId ? parseInt(subAgentId) : undefined,
          }),
      ...(job.job_type === 'task' && {
        prompt: taskPrompt.trim() ? taskPrompt.trim() : null,
      }),
      ...(job.job_type === 'watch' && {
        check_tool: watch.check_tool || undefined,
        check_args: resolveArgs(watch).args ?? null,
        check_args_exprs:
          Object.keys(watch.check_args_exprs).length > 0 ? watch.check_args_exprs : null,
        // The two halves of one condition: the expression gates deterministically,
        // the judgement is the semantic stage on what it returned. Cleared halves are
        // sent as null so a stale one cannot silently keep deciding the job.
        cel_expr: watch.cel_expr.trim() || null,
        llm_condition: watch.llm_condition.trim() || null,
        destroy_after_trigger: watch.destroy_after_trigger,
        // Exclusive outcomes: an agent's reply replaces the notification, so sending both
        // would leave one of them dead.
        ...(watch.outcome === 'agent'
          ? { prompt: watch.prompt.trim() || null, notification_message: null }
          : { notification_message: watch.notification_message.trim() || null, prompt: null }),
      }),
      // Sent unconditionally, null included: omitting it on an emptied value would
      // make "in-app only" unreachable, since the backend only clears a field that
      // is explicitly present in the request.
      delivery_channel_id: deliveryChannel ? parseInt(deliveryChannel) : null,
      voice_call: voiceCall,
      // Only meaningful for a schedule change on a job somebody else also runs; the
      // backend ignores it otherwise and picks the narrowest effect itself. A fixed
      // trigger leaves 'everyone' as the only legal target.
      ...(others && (showScope || triggerFixed)
        ? { scope: triggerFixed ? 'everyone' : scope }
        : {}),
    };

    // A paused job does not run whatever you save, and nothing on the way out says so:
    // the fix you just made looks applied while the scheduler keeps skipping the job.
    // Ask, rather than saving into a job that will not act on it.
    if (!job.enabled) {
      setPendingSave(body);
      return;
    }

    mutation.mutate({ body, resume: false });
  }

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4 space-y-0">
        <div className="space-y-1">
          <CardTitle>Job configuration</CardTitle>
          <CardDescription>
            {editing
              ? canWrite
                ? 'Editing — change the fields below, then save.'
                : `Editing. ${job.owner_email ?? 'The owner'} owns what this job does; you can change your own schedule and where its results go.`
              : 'Read-only. Click Edit configuration to make changes.'}
          </CardDescription>
        </div>
        <div className="flex shrink-0 gap-2">
          {editing ? (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  resetForm();
                  setEditing(false);
                }}
                disabled={mutation.isPending}
              >
                <Undo2 className="mr-1.5 h-4 w-4" />
                Discard
              </Button>
              <Button size="sm" onClick={handleSave} disabled={!dirty || mutation.isPending}>
                <Save className="mr-1.5 h-4 w-4" />
                {mutation.isPending ? 'Saving…' : 'Save changes'}
              </Button>
            </>
          ) : (
            <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
              <Pencil className="mr-1.5 h-4 w-4" />
              Edit configuration
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <fieldset disabled={!editing} className="m-0 grid min-w-0 gap-4 border-0 p-0">
          {/* What the job DOES belongs to the definition, so a reader may look but not
              touch it. One boundary rather than a per-field list: the backend's rule is
              the same shape — a reader may change nothing on the definition at all. */}
          <fieldset disabled={!canWrite} className="m-0 grid min-w-0 gap-4 border-0 p-0">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="grid gap-1.5">
                <Label>Name</Label>
                <Input
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    touch();
                  }}
                />
              </div>
              <div className="grid gap-1.5">
                <Label>Max failures before pause</Label>
                <Input
                  type="number"
                  min={1}
                  max={20}
                  value={maxFailures}
                  onChange={(e) => {
                    setMaxFailures(parseInt(e.target.value) || 3);
                    touch();
                  }}
                />
              </div>
            </div>
          </fieldset>

          {/* The schedule is the one field group that can belong to either side. */}
          <fieldset disabled={!canEditTrigger} className="m-0 grid min-w-0 gap-4 border-0 p-0">
            {showScope && (
              <div className="grid gap-1.5">
                <Label>A schedule change applies to</Label>
                <Select value={scope} onValueChange={(v) => setScope(v as 'mine' | 'everyone')}>
                  <SelectTrigger className="sm:w-[320px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="mine">Just me</SelectItem>
                    <SelectItem value="everyone">Everyone's default</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {scope === 'mine'
                    ? 'Only your own runs move. The other subscribers keep theirs.'
                    : job.trigger_inherited
                      ? `Changes the job's default, so every subscriber who has not set their own schedule follows it — including you.`
                      : // Saying "including you" is not enough for someone who has their
                        // own schedule: what happens to them is that they LOSE it, and
                        // that is the half they would not predict.
                        `Changes the job's default, so every subscriber who has not set their own schedule follows it. Your own schedule is dropped and you follow the new default too.`}
                </p>
              </div>
            )}
            {others && !showScope && canWrite && triggerFixed && (
              <p className="text-xs text-muted-foreground">
                This job's schedule is fixed, so a change applies to every subscriber.
              </p>
            )}
            {sharedTrigger && !canWrite && (
              <p className="text-xs text-muted-foreground">
                {triggerFixed
                  ? `The schedule is fixed by ${job.owner_email ?? 'the owner'} and cannot be changed here.`
                  : job.trigger_inherited
                    ? `You follow the job's default schedule, including later changes to it. Changing it here makes it yours alone.`
                    : `This is your own schedule. The job's default is ${defaultScheduleLabel(job)}.`}
              </p>
            )}
            {sharedTrigger && canWrite && !job.trigger_inherited && (
              <p className="text-xs text-muted-foreground">
                You run on your own schedule; the job's default is {defaultScheduleLabel(job)}.
              </p>
            )}
            {/* Leaving the default is one click; without this, coming back was a favour
                only the owner could do — and only for everybody at once. */}
            {sharedTrigger && !triggerFixed && !job.trigger_inherited && (
              <div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={followDefault.isPending}
                  onClick={() => followDefault.mutate({ path: { job_id: job.id } })}
                >
                  <RotateCcw className="mr-1.5 h-4 w-4" />
                  Follow the job's default again
                </Button>
              </div>
            )}

            {job.schedule_kind === 'cron' && (
              <CronField
                value={cronExpr}
                onChange={(v) => {
                  setCronExpr(v);
                  touch();
                }}
                timezone={job.timezone}
              />
            )}

            {job.schedule_kind === 'interval' && (
              <div className="grid gap-1.5">
                <Label>Interval (seconds)</Label>
                <Input
                  type="number"
                  min={60}
                  value={intervalSeconds}
                  onChange={(e) => {
                    setIntervalSeconds(e.target.value);
                    touch();
                  }}
                />
              </div>
            )}

            {job.schedule_kind === 'once' && (
              <div className="grid gap-1.5">
                <Label>Run at</Label>
                <Input
                  type="datetime-local"
                  min={nowDatetimeLocal(job.timezone)}
                  value={runAt}
                  onChange={(e) => {
                    setRunAt(e.target.value);
                    touch();
                  }}
                />
                {job.timezone && (
                  <p className="text-xs text-muted-foreground">Interpreted in {job.timezone}</p>
                )}
              </div>
            )}
          </fieldset>

          {/* Everything from here to the delivery channel is definition-owned too. */}
          <fieldset disabled={!canWrite} className="m-0 grid min-w-0 gap-4 border-0 p-0">
          {/* Sub-agent picker (task jobs) */}
          {job.job_type === 'task' && (
            <>
              <div className="grid gap-1.5">
                <Label>Sub-agent</Label>
                <SubAgentSelect
                  value={subAgentId}
                  onChange={(v) => {
                    setSubAgentId(v);
                    touch();
                  }}
                  subAgents={subAgents}
                  disabled={!editing || !canWrite}
                />
                <p className="text-xs text-muted-foreground">
                  {subAgents.find((sa) => sa.id === parseInt(subAgentId))?.type === 'automated'
                    ? 'This automated sub-agent has a predefined system prompt.'
                    : 'Select a sub-agent to execute for this scheduled job.'}
                </p>
              </div>

              {/* Task instruction - always shown for task jobs */}
              <div className="grid gap-1.5">
                <Label>
                  Task instruction <span className="text-muted-foreground text-xs">(optional)</span>
                </Label>
                <Textarea
                  rows={3}
                  value={taskPrompt}
                  onChange={(e) => {
                    setTaskPrompt(e.target.value);
                    touch();
                  }}
                  placeholder="Specific task or instruction for this execution (leave empty for default behavior)…"
                />
                <p className="text-xs text-muted-foreground">
                  {subAgents.find((sa) => sa.id === parseInt(subAgentId))?.type === 'automated'
                    ? 'Optional task-specific instruction. If empty, the agent will follow its configured system prompt.'
                    : 'This instruction will be sent to the sub-agent. If empty, defaults to "Execute your configured task."'}
                </p>
              </div>
            </>
          )}

          {/* Watch-specific fields */}
          {job.job_type === 'watch' && (
            <>
              {/* Describe-the-job entry point, above the fields it writes into. */}
              {editing && canWrite && (
                <div className="bg-muted grid gap-2.5 rounded-md border p-3.5">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Sparkles className="size-3.5" />
                    <span className="text-[13px] font-semibold">Describe the change</span>
                    <span className="text-muted-foreground text-xs">
                      rewrites the fields below — review before saving
                    </span>
                  </div>
                  <div className="flex gap-2">
                    <Input
                      className="bg-background flex-1"
                      placeholder="e.g. also tell me when a meeting is cancelled"
                      value={aiQuery}
                      onChange={(e) => setAiQuery(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && handleAiGenerate()}
                    />
                    <Button
                      type="button"
                      disabled={!aiQuery.trim() || aiLoading}
                      onClick={handleAiGenerate}
                    >
                      {aiLoading ? <Loader2 className="size-4 animate-spin" /> : 'Generate'}
                    </Button>
                  </div>
                </div>
              )}

              {/* The same fields the create dialog renders. `read` shows values as text;
                  the check only runs while editing, since it is a real call. */}
              <WatchFields
                mode={editing && canWrite ? 'edit' : 'read'}
                value={watch}
                onChange={(next) => {
                  setWatch((w) => ({ ...w, ...next }));
                  touch();
                }}
                mcpTools={mcpTools}
                subAgents={subAgents}
                storedResult={job.last_check_result as Record<string, unknown> | null}
                onError={setError}
              />
            </>
          )}

      {/* Voice call toggle. Not task-only any more: the scheduler evaluates a watch's
          condition before dispatching, so a call happens because something happened. */}
          <div className="flex items-center gap-3 rounded-lg border px-3 py-2">
            <Switch
              id="voice-call-edit"
              checked={voiceCall}
              onCheckedChange={(v) => {
                setVoiceCall(v);
                touch();
              }}
              disabled={!editing || !canWrite}
            />
            <Label htmlFor="voice-call-edit" className="cursor-pointer text-sm">
              Deliver as a phone call
            </Label>
            <span className="text-xs text-muted-foreground">
              When enabled, the agent response is delivered as a phone call instead of a text message.
            </span>
          </div>

          </fieldset>

          {/* Delivery channel — the subscriber's own, on a shared job as on any other:
              a run of a job someone else authored still lands where THIS person reads. */}
          <div className="grid gap-1.5">
            <Label>Delivery channel</Label>
            {/* "_none" is a sentinel: a SelectItem cannot carry an empty value, so the
                absence of a channel needs a value of its own to be selectable at all. */}
            <Select
              value={deliveryChannel || '_none'}
              disabled={!editing}
              onValueChange={(v) => {
                setDeliveryChannel(v === '_none' ? '' : v);
                touch();
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder="None (in-app notifications only)" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="_none">
                  <span className="text-muted-foreground">None (in-app only)</span>
                </SelectItem>
                {channels.length === 0 ? (
                  <div className="px-3 py-2 text-sm text-muted-foreground">No delivery channels registered</div>
                ) : (
                  channels.map((ch) => (
                    <SelectItem key={ch.id} value={String(ch.id)}>
                      {ch.name}
                      {ch.description && <span className="ml-2 text-xs text-muted-foreground">— {ch.description}</span>}
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}
        </fieldset>

        {/* Read-only info — always visible */}
        <div className="mt-4 grid gap-2 rounded-lg bg-muted/30 p-3 text-sm text-muted-foreground sm:grid-cols-2">
          <div>
            <span className="font-medium text-foreground">Created:</span> {formatDate(job.created_at)}
          </div>
          <div>
            <span className="font-medium text-foreground">Last updated:</span> {formatDate(job.updated_at)}
          </div>
          <div>
            <span className="font-medium text-foreground">Next run:</span>{' '}
            {/* A paused job's stored next_run_at is a leftover: resuming recomputes it.
                Printing it anyway is how a paused job reads as one that is about to run. */}
            {job.enabled ? formatDate(job.next_run_at) : '— paused'}
          </div>
          <div>
            <span className="font-medium text-foreground">Consecutive failures:</span> {job.consecutive_failures}
          </div>
        </div>

        {/* Saving a paused job stores an edit the scheduler will not act on, and nothing
            downstream says so. Ask instead of letting the fix look applied. */}
        <AlertDialog open={pendingSave !== null} onOpenChange={(open) => !open && setPendingSave(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>This job is paused</AlertDialogTitle>
              <AlertDialogDescription>
                <strong>{job.name}</strong> is paused{job.paused_reason ? ` (${job.paused_reason})` : ''}, so it
                will not run on its schedule whatever you save. Resume it now, or keep it paused and resume it
                later.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={mutation.isPending}>Cancel</AlertDialogCancel>
              <Button
                variant="outline"
                disabled={mutation.isPending}
                onClick={() => {
                  const body = pendingSave;
                  setPendingSave(null);
                  if (body) mutation.mutate({ body, resume: false });
                }}
              >
                Save, keep paused
              </Button>
              <Button
                disabled={mutation.isPending}
                onClick={() => {
                  const body = pendingSave;
                  setPendingSave(null);
                  if (body) mutation.mutate({ body, resume: true });
                }}
              >
                Save and resume
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// The ask a parked run is waiting on
// ---------------------------------------------------------------------------

/** The authorize URL and the service it belongs to, out of the stored ask. */
function readParkedAsk(run: ScheduledJobRun): { authUrl?: string; subject?: string } | null {
  const payload = run.parked_payload as
    | { auth_requirement?: { service?: string; resource?: string; auth_methods?: { auth_url?: string }[] } }
    | null
    | undefined;
  const requirement = payload?.auth_requirement;
  if (!requirement) return null;
  const withUrl = (requirement.auth_methods ?? []).find((m) => !!m?.auth_url);
  return {
    ...(withUrl?.auth_url ? { authUrl: withUrl.auth_url } : {}),
    ...(requirement.service || requirement.resource
      ? { subject: requirement.service || requirement.resource }
      : {}),
  };
}

/**
 * Offer the owner the way out of a stopped job.
 *
 * A parked run holds the job's schedule, so this is not decoration: until it is
 * answered the job does not run at all. The chat client that delivered the ask is
 * where most owners will answer it, but an owner who came here instead should not be
 * sent away to find a message.
 */
function ParkedRunNotice({ jobId, run }: { jobId: number; run: ScheduledJobRun }) {
  const qc = useQueryClient();
  const [pending, setPending] = useState<'approved' | 'declined' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ask = readParkedAsk(run);

  const answer = async (decision: 'approved' | 'declined') => {
    setPending(decision);
    setError(null);
    try {
      await resumeParkedRun(jobId, run.id, decision);
      // `jobId` is a number (parseInt of the route param) and the query keys hold it as
      // one. Passing String(jobId) here matched nothing, so the ask stayed on screen
      // until the 15s poll happened to refresh it — which reads as the button not
      // working, on the one control whose whole job is to unblock a stopped job.
      qc.invalidateQueries({ queryKey: ['scheduler-job', jobId] });
      qc.invalidateQueries({ queryKey: ['scheduler-runs', jobId] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="mb-4 rounded-lg border border-amber-500/50 bg-amber-50/50 p-4 dark:bg-amber-950/20">
      <div className="flex items-start gap-3">
        <KeyRound className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
        <div className="flex-1 space-y-2">
          <p className="text-sm font-medium">
            {ask?.subject
              ? `This job needs your permission to use ${ask.subject}.`
              : 'This job needs your permission before it can continue.'}
          </p>
          <p className="text-xs text-muted-foreground">
            It will not run again until you answer. Authorize in your browser, then confirm here.
          </p>
          {/* Name the run this belongs to. The notice is deliberately a banner and not a
              row control — the job not running at all is a fact about the JOB, and in a
              row you would only find it by scrolling the history. But a banner that names
              no occurrence is ambiguous the moment more than one run is involved, which
              is exactly how it read when an older parked run was the one still waiting. */}
          <p className="text-xs text-muted-foreground">
            Waiting since {formatDate(run.started_at)} · run #{run.id}
          </p>
          {error && <p className="text-xs text-destructive">{error}</p>}
          <div className="flex flex-wrap gap-2 pt-1">
            {ask?.authUrl && (
              <Button asChild size="sm" variant="default">
                <a href={ask.authUrl} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="mr-1 h-3 w-3" /> Authorize
                </a>
              </Button>
            )}
            <Button size="sm" variant="outline" disabled={pending !== null} onClick={() => answer('approved')}>
              {pending === 'approved' && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
              Done, continue
            </Button>
            <Button size="sm" variant="ghost" disabled={pending !== null} onClick={() => answer('declined')}>
              {pending === 'declined' && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
              Don't allow
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Run history table
// ---------------------------------------------------------------------------

function RunHistoryTable({ runs }: { runs: ScheduledJobRun[] }) {
  const { isAdmin } = useAuth();

  if (runs.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed py-8 text-center">
        <Clock className="h-6 w-6 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">No runs yet</p>
        <p className="text-xs text-muted-foreground max-w-sm">
          Use the <strong>Run now</strong> button above to trigger a test run and verify your job works as expected.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b bg-muted/50">
            <th className="px-4 py-3 text-left font-medium">Started</th>
            <th className="px-4 py-3 text-left font-medium">Duration</th>
            <th className="px-4 py-3 text-left font-medium">Status</th>
            <th className="px-4 py-3 text-left font-medium">Result</th>
            <th className="px-4 py-3 text-center font-medium">Webhook</th>
            <th className="px-4 py-3 text-center font-medium">Usage</th>
            {isAdmin && <th className="px-4 py-3 text-center font-medium">Trace</th>}
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-b last:border-0 hover:bg-muted/30">
              <td className="px-4 py-3 text-muted-foreground">{formatDate(run.started_at)}</td>
              <td className="px-4 py-3 text-muted-foreground">{formatDuration(run.started_at, run.completed_at)}</td>
              <td className="px-4 py-3">
                <RunStatusBadge run={run} />
              </td>
              <td className="px-4 py-3 max-w-xs">
                {run.status === 'failed' && run.error_message ? (
                  <span className="text-destructive text-xs line-clamp-2">{run.error_message}</span>
                ) : (
                  <span className="text-muted-foreground text-xs line-clamp-2">{run.result_summary ?? '—'}</span>
                )}
              </td>
              <td className="px-4 py-3 text-center">
                {run.delivered ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <div>
                        <Send className="mx-auto h-4 w-4 text-muted-foreground" />
                      </div>
                    </TooltipTrigger>
                    <TooltipContent>Notification sent (best effort — delivery receipt not confirmed)</TooltipContent>
                  </Tooltip>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
              <td className="px-4 py-3 text-center">
                {run.conversation_id ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <a
                        href={`/app/usage?conversation_id=${run.conversation_id}`}
                        className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                      >
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    </TooltipTrigger>
                    <TooltipContent>View usage logs for this run</TooltipContent>
                  </Tooltip>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
              {isAdmin && (
                <td className="px-4 py-3 text-center">
                  {run.conversation_id ? (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <a
                          href={`https://eu.smith.langchain.com/o/${config.langsmith.organizationId}/projects/p/${config.langsmith.projectId}/t/${run.conversation_id}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                        >
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      </TooltipTrigger>
                      <TooltipContent>View trace in LangSmith</TooltipContent>
                    </Tooltip>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function SchedulerJobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const jobId = parseInt(id ?? '0', 10);

  const enabled = !isNaN(jobId) && jobId > 0;

  const [runNowLoading, setRunNowLoading] = useState(false);
  const [runNowRunId, setRunNowRunId] = useState<number | null>(null);
  const [runNowResult, setRunNowResult] = useState<RunNowResult | null>(null);
  const [runNowError, setRunNowError] = useState<string | null>(null);
  const [showDelete, setShowDelete] = useState(false);

  async function handleRunNow() {
    setRunNowLoading(true);
    setRunNowRunId(null);
    setRunNowResult(null);
    setRunNowError(null);
    try {
      const { run_id } = await runJobNow(jobId);
      setRunNowRunId(run_id);
      // Keep runNowLoading=true — the scheduler_notification WebSocket event
      // will deliver the result and clear the loading state.
    } catch (e) {
      setRunNowError(e instanceof Error ? e.message : String(e));
      setRunNowLoading(false);
    }
  }

  useEffect(() => {
    const socket = io({ path: '/api/v1/socket.io' });
    socket.on('scheduler_notification', (data: SchedulerNotification) => {
      if (data.job_id === jobId) {
        setRunNowResult(data);
        setRunNowLoading(false);
        setRunNowRunId(null);
        qc.invalidateQueries({ queryKey: ['scheduler-job', jobId] });
        qc.invalidateQueries({ queryKey: ['scheduler-runs', jobId] });
      }
    });
    return () => {
      socket.disconnect();
    };
  }, [jobId, qc]);

  const {
    data: job,
    isLoading: jobLoading,
    error: jobError,
  } = useQuery({
    queryKey: ['scheduler-job', jobId],
    queryFn: () => getJob(jobId),
    enabled,
  });

  const { data: runs = [], isLoading: runsLoading } = useQuery({
    queryKey: ['scheduler-runs', jobId],
    queryFn: () => listRuns(jobId),
    enabled,
    refetchInterval: 15_000, // refresh run history every 15s
  });

  // At most one run of a job is ever parked (claim_due_jobs will not claim a job while
  // one is), so the first match is the one waiting — and the reason the job is idle.
  //
  // `parked_task_id` is what makes it still ANSWERABLE, not the status: an answered run
  // keeps `auth_required` forever, because that is a true record of how that occurrence
  // ended. Keying the card on status alone left it on screen after the answer, and a
  // second click hit a task that had since gone terminal.
  const parkedRun = runs.find((r) => r.status === 'auth_required' && r.parked_task_id);

  const pauseMutation = useMutation({
    mutationFn: () => pauseJob(jobId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['scheduler-job', jobId] }),
  });

  const resumeMutation = useMutation({
    mutationFn: () => resumeJob(jobId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['scheduler-job', jobId] }),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteJob(jobId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['scheduler-jobs'] });
      navigate('/app/scheduler');
    },
  });

  function handleDelete() {
    setShowDelete(true);
  }

  return (
    <div className="flex flex-col gap-6 p-4">
      {/* Back button */}
      <Button variant="ghost" size="sm" className="-ml-1 w-fit" onClick={() => navigate('/app/scheduler')}>
        <ArrowLeft className="mr-1.5 h-4 w-4" />
        Back to Scheduler
      </Button>

      {jobLoading && <DetailSkeleton />}

      {jobError && (
        <div className="flex items-center gap-2 text-destructive text-sm">
          <XCircle className="h-4 w-4" />
          Failed to load job
        </div>
      )}

      {job && (
        <>
          <JobHeader
            job={job}
            onPause={() => pauseMutation.mutate()}
            onResume={() => resumeMutation.mutate()}
            onDelete={handleDelete}
            onRunNow={handleRunNow}
            isPendingPause={pauseMutation.isPending}
            isPendingResume={resumeMutation.isPending}
            isPendingDelete={deleteMutation.isPending}
            isRunningNow={runNowLoading}
          />

          {/* Run-now result banner */}
          {(runNowResult || runNowError || runNowLoading) && (
            <div
              className={`rounded-md border px-4 py-3 text-sm ${
                runNowError
                  ? 'border-destructive/40 bg-destructive/5 text-destructive'
                  : runNowResult?.status === 'failed'
                    ? 'border-destructive/40 bg-destructive/5 text-destructive'
                    : runNowResult?.status === 'success'
                      ? 'border-green-500/40 bg-green-500/5 text-green-700 dark:text-green-400'
                      : runNowResult?.status === 'condition_not_met'
                        ? 'border-yellow-500/40 bg-yellow-500/5 text-yellow-700 dark:text-yellow-400'
                        : 'border-border bg-muted/30'
              }`}
            >
              {runNowLoading && !runNowResult && !runNowError ? (
                <div className="flex items-center gap-2">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>Dispatched{runNowRunId ? ` (run #${runNowRunId})` : ''} — waiting for result…</span>
                </div>
              ) : runNowError ? (
                <p>
                  <strong>Run failed:</strong> {runNowError}
                </p>
              ) : (
                runNowResult && (
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <RunStatusBadge run={runNowResult} />
                      {runNowResult.result_summary && <span className="text-xs">{runNowResult.result_summary}</span>}
                      {runNowResult.error_message && <span className="text-xs">{runNowResult.error_message}</span>}
                    </div>
                    <span className="text-xs text-muted-foreground">
                      {runNowResult.delivered ? '↗ Webhook notified (best effort)' : '○ No webhook configured'}
                    </span>
                  </div>
                )
              )}
            </div>
          )}

          {/* Why the last run did what it did. Above the configuration because on this
              page that is the question being asked, and the answer is already stored. */}
          {job.job_type === 'watch' && (
            <LastCheckPanel
              run={runs.find((r) => r.condition_evaluation) ?? runs[0]}
              result={job.last_check_result as Record<string, unknown> | null}
            />
          )}

          {/* Keyed on the trigger the server reports, so a schedule that changed under
              the form — dropping your own schedule, or the owner resetting everyone's —
              remounts it on the new values. Without this the form keeps the old
              override in `useState` and the next save resends it, quietly recreating the
              override the user just cleared. */}
          <EditForm key={triggerIdentity(job)} job={job} />

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                Run history
                {runsLoading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {parkedRun && <ParkedRunNotice jobId={Number(jobId)} run={parkedRun} />}
              <RunHistoryTable runs={runs} />
            </CardContent>
          </Card>

          {/* Delete confirmation */}
          <AlertDialog open={showDelete} onOpenChange={setShowDelete}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Delete scheduled job?</AlertDialogTitle>
                <AlertDialogDescription>
                  The job <strong>{job.name}</strong> will be permanently deleted. This action cannot be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={deleteMutation.isPending}>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                  disabled={deleteMutation.isPending}
                  onClick={() => deleteMutation.mutate()}
                >
                  {deleteMutation.isPending ? (
                    'Deleting…'
                  ) : (
                    <>
                      <Trash2 className="mr-1.5 h-4 w-4" />
                      Delete
                    </>
                  )}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </>
      )}
    </div>
  );
}
