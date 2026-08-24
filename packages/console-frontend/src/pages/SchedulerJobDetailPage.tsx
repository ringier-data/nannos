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
import { resolveArgs } from '@/lib/watchArgs';
import { config } from '@/config';
import {
  type JobRunStatus,
  type ScheduledJob,
  type ScheduledJobRun,
  getDeliveryChannels,
  generateJobDraft,
  updateScheduledJob,
  runJobNow,
  type DeliveryChannel,
  getJob,
  listRuns,
  pauseJob,
  resumeJob,
  deleteJob,
} from '@/api/scheduler';
import { consoleListSubAgentsOptions, consoleListMcpToolsOptions } from '@/api/generated/@tanstack/react-query.gen';
import { useAuth } from '@/contexts/AuthContext';
import { CronField } from '@/components/CronField';
import { describeCron } from '@/lib/cron';
import { DetailSkeleton } from '@/components/skeletons';
import { io } from 'socket.io-client';

interface SchedulerNotification {
  job_id: number;
  job_name: string;
  run_id: number;
  status: JobRunStatus;
  result_summary?: string;
  error_message?: string;
  timestamp: string;
}

interface RunNowResult {
  status: JobRunStatus;
  result_summary?: string | null;
  error_message?: string | null;
  delivered?: boolean | null;
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

function RunStatusBadge({ status }: { status: ScheduledJobRun['status'] }) {
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
          <span>·</span>
          {job.enabled ? (
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
          <Button variant="outline" size="sm" disabled={isPendingPause} onClick={onPause}>
            <Pause className="mr-1.5 h-4 w-4" />
            Pause
          </Button>
        ) : (
          <Button variant="outline" size="sm" disabled={isPendingResume} onClick={onResume}>
            <Play className="mr-1.5 h-4 w-4" />
            Resume
          </Button>
        )}
        <Button variant="destructive" size="sm" className="ml-2" disabled={isPendingDelete} onClick={onDelete}>
          <Trash2 className="mr-1.5 h-4 w-4" />
          Delete
        </Button>
      </div>
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

  // Pre-select first channel once loaded (only if no channel is already set)
  useEffect(() => {
    if (channels.length > 0 && !deliveryChannel) {
      setDeliveryChannel(String(channels[0].id));
    }
  }, [channels]); // eslint-disable-line react-hooks/exhaustive-deps


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
      const draft = await generateJobDraft(mcpTools as unknown as Record<string, unknown>[], aiQuery);
      setWatch((w) => {
        const next = { ...w };
        if (draft.check_tool) next.check_tool = draft.check_tool;
        if (draft.check_args) {
          next.check_args = draft.check_args as Record<string, unknown>;
          next.check_args_text = JSON.stringify(draft.check_args, null, 2);
          next.args_mode = 'fields';
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
    } catch {
      setError('AI generation failed. Please edit the fields manually.');
    } finally {
      setAiLoading(false);
    }
  }

  // ── Save ──────────────────────────────────────────────────────────────────
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => updateScheduledJob(job.id, body),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ['scheduler-job', job.id],
      });
      setDirty(false);
      setEditing(false);
    },
    onError: (e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    },
  });

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
        ? watch.sub_agent_id !== initialSubAgentId && {
            sub_agent_id:
              watch.outcome === 'agent' && watch.sub_agent_id ? parseInt(watch.sub_agent_id) : null,
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
      ...(deliveryChannel && { delivery_channel_id: parseInt(deliveryChannel) }),
      voice_call: voiceCall,
    };

    mutation.mutate(body);
  }

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4 space-y-0">
        <div className="space-y-1">
          <CardTitle>Job configuration</CardTitle>
          <CardDescription>
            {editing
              ? 'Editing — change the fields below, then save.'
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
              {job.timezone && <p className="text-xs text-muted-foreground">Interpreted in {job.timezone}</p>}
            </div>
          )}

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
                  disabled={!editing}
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
              {editing && (
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
                      disabled={!aiQuery.trim() || aiLoading || mcpTools.length === 0}
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
                mode={editing ? 'edit' : 'read'}
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
              disabled={!editing}
            />
            <Label htmlFor="voice-call-edit" className="cursor-pointer text-sm">
              Deliver as a phone call
            </Label>
            <span className="text-xs text-muted-foreground">
              When enabled, the agent response is delivered as a phone call instead of a text message.
            </span>
          </div>

          {/* Delivery channel */}
          <div className="grid gap-1.5">
            <Label>Delivery channel</Label>
            <Select
              value={deliveryChannel}
              disabled={!editing}
              onValueChange={(v) => {
                setDeliveryChannel(v);
                touch();
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder="Select a delivery channel…" />
              </SelectTrigger>
              <SelectContent>
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
            <span className="font-medium text-foreground">Next run:</span> {formatDate(job.next_run_at)}
          </div>
          <div>
            <span className="font-medium text-foreground">Consecutive failures:</span> {job.consecutive_failures}
          </div>
        </div>
      </CardContent>
    </Card>
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
                <RunStatusBadge status={run.status} />
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
                      <RunStatusBadge status={runNowResult.status} />
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

          <EditForm job={job} />

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                Run history
                {runsLoading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
              </CardTitle>
            </CardHeader>
            <CardContent>
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
