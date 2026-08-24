import { useState } from 'react';
import { useNavigate } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Plus,
  Calendar,
  Play,
  Pause,
  Trash2,
  AlertCircle,
  CheckCircle2,
  Clock,
  ChevronRight,
  Sparkles,
  Loader2,
  Undo2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { TableSkeleton } from '@/components/skeletons';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import {
  type ScheduledJob,
  type JobType,
  type ScheduleKind,
  type ScheduledJobCreateExtended,
  getDeliveryChannels,
  generateJobDraft,
  createScheduledJob,
  type DeliveryChannel,
  listJobs,
  pauseJob,
  resumeJob,
  deleteJob,
} from '@/api/scheduler';
import {
  consoleListSubAgentsOptions,
  consoleListMcpToolsOptions,
  getCurrentUserSettingsApiV1AuthMeSettingsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import { CronField } from '@/components/CronField';
import { AgentActionFields } from '@/components/AgentActionFields';
import { agentActionError, automatedSubAgentParameters } from '@/lib/agentAction';
import { argsModeFor, missingRequiredArgs, resolveArgs } from '@/lib/watchArgs';
import { WatchFields } from '@/components/WatchFields';
import { describeCron } from '@/lib/cron';
import { AiBadge, FieldError, SectionHeader } from '@/components/formChrome';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDateRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  const now = new Date();
  const diff = d.getTime() - now.getTime();
  const abs = Math.abs(diff);
  const mins = Math.floor(abs / 60_000);
  const hrs = Math.floor(abs / 3_600_000);
  const days = Math.floor(abs / 86_400_000);
  const future = diff > 0;
  if (mins < 1) return 'just now';
  if (mins < 60) return future ? `in ${mins}m` : `${mins}m ago`;
  if (hrs < 24) return future ? `in ${hrs}h` : `${hrs}h ago`;
  return future ? `in ${days}d` : `${days}d ago`;
}

function scheduleLabel(job: ScheduledJob): string {
  if (job.schedule_kind === 'cron') return job.cron_expr ?? '—';
  if (job.schedule_kind === 'interval')
    return job.interval_seconds ? `every ${job.interval_seconds}s` : '—';
  if (job.schedule_kind === 'once' && job.run_at)
    return new Date(job.run_at).toLocaleString();
  return '—';
}

// ---------------------------------------------------------------------------
// Job status badge
// ---------------------------------------------------------------------------

function StatusBadge({ job }: { job: ScheduledJob }) {
  if (!job.enabled)
    return (
      <Badge variant="secondary" className="gap-1">
        <Pause className="h-3 w-3" /> Paused
      </Badge>
    );
  if (job.consecutive_failures > 0)
    return (
      <Badge variant="destructive" className="gap-1">
        <AlertCircle className="h-3 w-3" /> {job.consecutive_failures} failure
        {job.consecutive_failures > 1 ? 's' : ''}
      </Badge>
    );
  return (
    <Badge variant="default" className="gap-1 bg-green-600 hover:bg-green-600">
      <CheckCircle2 className="h-3 w-3" /> Active
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// Create job form
// ---------------------------------------------------------------------------

/** Date-time string in YYYY-MM-DDTHH:mm format suitable for datetime-local input, clamped to "now".
 * The submitted naive value is interpreted in the job's timezone on the backend, so the
 * clamp must be "now" as that timezone's wall-clock, not the browser's. */
function nowDatetimeLocal(timeZone?: string | null): string {
  const d = new Date();
  d.setSeconds(0, 0);
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

type SubAgentMode = 'existing' | 'automated';

interface CreateJobForm {
  name: string;
  job_type: JobType;
  schedule_kind: ScheduleKind;
  cron_expr: string;
  interval_seconds: string;
  run_at: string;
  // Mode selector for task jobs
  sub_agent_mode: SubAgentMode;
  // Existing sub-agent mode
  sub_agent_id: string;
  // Voice call mode: dispatch job via voice-agent
  voice_call: boolean;
  // Automated sub-agent mode
  automated_name: string;
  automated_description: string;
  automated_model: string;
  automated_system_prompt: string;
  automated_mcp_tools: string[]; // MCP tool names
  automated_enable_thinking: boolean;
  automated_thinking_level: string;
  // Task-specific
  prompt: string;
  // Watch-specific
  notification_message: string;
  check_tool: string;
  /** Arguments as edited through the schema-generated fields. */
  check_args: Record<string, unknown>;
  /** Arguments as edited raw. Authoritative while args_mode is 'json'. */
  check_args_text: string;
  args_mode: 'fields' | 'json';
  /** Per-argument CEL expressions (`= …` values), resolved and merged on every run. */
  check_args_exprs: Record<string, string>;
  /** CEL expression: extracts the evidence and gates the trigger in one. Optional. */
  cel_expr: string;
  /** Judged by a model — alone over the whole response, or on what cel_expr returned. */
  llm_condition: string;
  /** Notify, or hand the result to a sub-agent. Mutually exclusive. */
  outcome: 'notify' | 'agent';
  destroy_after_trigger: boolean;
  delivery_channel: string;
}

const defaultForm: CreateJobForm = {
  name: '',
  job_type: 'task',
  schedule_kind: 'cron',
  cron_expr: '',
  interval_seconds: '',
  run_at: '',
  sub_agent_mode: 'existing',
  sub_agent_id: '',
  voice_call: false,
  automated_name: '',
  automated_description: '',
  // Holds a unified model selection: a capability tier encoded as `tier:<tier>`, or a concrete
  // alias. Defaults to the standard tier (follows the fleet default → never a retired alias).
  automated_model: 'tier:standard',
  automated_system_prompt: '',
  automated_mcp_tools: [],
  automated_enable_thinking: false,
  automated_thinking_level: 'low',
  prompt: '',
  notification_message: '',
  check_tool: '',
  check_args: {},
  check_args_text: '',
  args_mode: 'fields',
  check_args_exprs: {},
  cel_expr: '',
  llm_condition: '',
  outcome: 'notify',
  destroy_after_trigger: true,
  delivery_channel: '',
};

function CreateJobDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (jobId: number) => void;
}) {
  const [form, setForm] = useState<CreateJobForm>({ ...defaultForm });
  const [error, setError] = useState<string | null>(null);

  const [aiQuery, setAiQuery] = useState('');
  const [aiLoading, setAiLoading] = useState(false);
  /** Field keys the last AI fill wrote, so each one can say where its value came from. */
  const [aiFilled, setAiFilled] = useState<Set<string>>(new Set());
  /** Form state from just before the AI fill, for a one-step undo. */
  const [aiUndo, setAiUndo] = useState<CreateJobForm | null>(null);
  /** Per-field validation, so an error lands next to the field that caused it. */
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const qc = useQueryClient();

  // ── Data queries ──────────────────────────────────────────────────────────
  const { data: subAgentsData } = useQuery(
    // Not owned_only: a job may run any sub-agent shared with the user, and listing
    // only owned ones made a legitimately chosen agent render as "not in your list".
    consoleListSubAgentsOptions({}),
  );
  const subAgents = subAgentsData?.items ?? [];

  const { data: mcpToolsData } = useQuery(consoleListMcpToolsOptions());
  const mcpTools = mcpToolsData?.tools ?? [];

  // New jobs snapshot the user's settings timezone on the backend; show it in the preview.
  const { data: userSettings } = useQuery(getCurrentUserSettingsApiV1AuthMeSettingsGetOptions());
  const userTimezone = userSettings?.data.timezone;

  const { data: channels = [] } = useQuery<DeliveryChannel[]>({
    queryKey: ['delivery-channels'],
    queryFn: getDeliveryChannels,
    staleTime: 60_000,
  });

  // ── Helpers ───────────────────────────────────────────────────────────────
  function update<K extends keyof CreateJobForm>(key: K, value: CreateJobForm[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    setError(null);
  }

  /** Drop the AI marker from fields the user has just edited by hand. */
  function clearAi(...keys: string[]) {
    setAiFilled((prev) => {
      if (!keys.some((k) => prev.has(k))) return prev;
      const next = new Set(prev);
      keys.forEach((k) => next.delete(k));
      return next;
    });
  }

  function clearFieldError(...keys: string[]) {
    setFieldErrors((prev) => {
      if (!keys.some((k) => k in prev)) return prev;
      const next = { ...prev };
      keys.forEach((k) => delete next[k]);
      return next;
    });
  }

  // ── AI generation ─────────────────────────────────────────────────────────
  /**
   * Fill the form from one sentence.
   *
   * Scope is deliberately the whole form, not just the condition fields: the field
   * it cannot see is the one the user has to guess at. Every field written is marked
   * and the previous state kept, because values that appear without explanation are
   * values nobody trusts.
   */
  async function handleAiGenerate() {
    if (!aiQuery.trim()) return;
    setAiLoading(true);
    setError(null);
    try {
      const result = await generateJobDraft(
        mcpTools as unknown as Record<string, unknown>[],
        aiQuery,
      );
      const filled = new Set<string>();
      setAiUndo(form);
      setForm((f) => {
        const next = { ...f };
        // Name is only suggested into an empty field — overwriting a name the user
        // already chose would be the fill exceeding its remit. Everything else is
        // applied, because the whole point is that one sentence configures the job.
        if (result.name && !f.name.trim()) {
          next.name = result.name;
          filled.add('name');
        }
        // The job type decides what the rest of the form even means, so it is
        // applied first and the fields that do not survive the switch are reset.
        if (result.job_type && result.job_type !== f.job_type) {
          next.job_type = result.job_type;
          filled.add('job_type');
        }
        if (result.schedule_kind) {
          next.schedule_kind = result.schedule_kind;
          filled.add('schedule');
        }
        if (result.cron_expr) {
          next.cron_expr = result.cron_expr;
          next.schedule_kind = result.schedule_kind ?? 'cron';
          filled.add('schedule');
        }
        if (result.interval_seconds) {
          next.interval_seconds = String(result.interval_seconds);
          next.schedule_kind = result.schedule_kind ?? 'interval';
          filled.add('schedule');
        }
        if (result.run_at) {
          // datetime-local wants "YYYY-MM-DDTHH:mm"; the API answers ISO 8601.
          next.run_at = result.run_at.slice(0, 16);
          next.schedule_kind = result.schedule_kind ?? 'once';
          filled.add('schedule');
        }
        if (result.check_tool) {
          next.check_tool = result.check_tool;
          filled.add('check_tool');
        }
        if (result.check_args) {
          const args = result.check_args as Record<string, unknown>;
          next.check_args = args;
          next.check_args_text = JSON.stringify(args, null, 2);
          next.args_mode = argsModeFor(
            args,
            mcpTools.find((t) => t.name === result.check_tool),
          );
          filled.add('check_args');
        }
        if (result.check_args_exprs && Object.keys(result.check_args_exprs).length > 0) {
          next.check_args_exprs = result.check_args_exprs as Record<string, string>;
          filled.add('check_args');
        }
        if (result.cel_expr) {
          next.cel_expr = result.cel_expr;
          filled.add('cel_expr');
        }
        if (result.llm_condition) {
          next.llm_condition = result.llm_condition;
          filled.add('llm_condition');
        }
        // A sub-agent means the outcome is "run an agent"; the notification text is
        // then unused, so the two are applied as the exclusive choice they are.
        if (result.sub_agent_id) {
          next.sub_agent_id = String(result.sub_agent_id);
          next.outcome = 'agent';
          next.sub_agent_mode = 'existing';
          filled.add('sub_agent_id');
          if (result.prompt) {
            next.prompt = result.prompt;
            filled.add('prompt');
          }
        } else if (result.notification_message) {
          next.notification_message = result.notification_message;
          next.outcome = 'notify';
          filled.add('notification_message');
        }
        if (result.delivery_channel_id) {
          next.delivery_channel = String(result.delivery_channel_id);
          filled.add('delivery_channel');
        }
        if (result.destroy_after_trigger != null) {
          next.destroy_after_trigger = result.destroy_after_trigger;
          filled.add('destroy_after_trigger');
        }
        return next;
      });
      setAiFilled(filled);
      setFieldErrors({});
    } catch {
      setError('AI generation failed. Please fill in the fields manually.');
    } finally {
      setAiLoading(false);
    }
  }

  function undoAiFill() {
    if (!aiUndo) return;
    setForm(aiUndo);
    setAiUndo(null);
    setAiFilled(new Set());
    setFieldErrors({});
  }

  // ── Submission ────────────────────────────────────────────────────────────
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    // Schedule and name errors are per-field too, so nothing about what to fix is
    // left to a single sentence above the footer.
    const scheduleErrors: Record<string, string> = {};
    if (!form.name.trim()) scheduleErrors.name = 'Give the job a name.';
    if (form.schedule_kind === 'cron') {
      if (!form.cron_expr.trim()) scheduleErrors.cron_expr = 'A cron expression is required.';
      else if (!describeCron(form.cron_expr).ok)
        scheduleErrors.cron_expr = 'That is not a valid cron expression.';
    }
    if (form.schedule_kind === 'interval' && !form.interval_seconds)
      scheduleErrors.interval_seconds = 'An interval is required.';
    if (form.schedule_kind === 'once' && !form.run_at)
      scheduleErrors.run_at = 'A date and time is required.';
    if (Object.keys(scheduleErrors).length > 0) {
      setFieldErrors(scheduleErrors);
      return setError(null);
    }
    
    // Task job validations
    if (form.job_type === 'task') {
      const agentError = agentActionError(form);
      if (agentError) return setError(agentError);
      // Message is optional for all task jobs
    }
    
    // Watch job validations. Errors are collected per field rather than returned as
    // one string, so the user is told which control to fix instead of hunting for it.
    let check_args: Record<string, unknown> | undefined;
    if (form.job_type === 'watch') {
      const errors: Record<string, string> = {};
      if (!form.check_tool) errors.check_tool = 'Choose the tool this job should call.';

      const parsed = resolveArgs(form);
      if (parsed.error) errors.check_args = parsed.error;
      check_args = parsed.args;

      const selectedTool = mcpTools.find((t) => t.name === form.check_tool);
      const missing = missingRequiredArgs(selectedTool, form, check_args);
      if (missing.size > 0) {
        errors.check_args = `Fill in ${[...missing].join(', ')}.`;
      }

      if (!form.cel_expr.trim() && !form.llm_condition.trim()) {
        // A watch needs something to decide with; either half of the condition works.
        errors.cel_expr = 'Write an expression, a condition for the model to judge, or both.';
      }

      if (Object.keys(errors).length > 0) {
        setFieldErrors(errors);
        return setError(null);
      }
      setFieldErrors({});

      // Held to the same standard as a task's, now that a watch can carry one.
      if (form.outcome === 'agent') {
        const agentError = agentActionError(form);
        if (agentError) return setError(agentError);
      }
    }

    const body: ScheduledJobCreateExtended = {
      name: form.name.trim(),
      job_type: form.job_type,
      schedule_kind: form.schedule_kind,
      ...(form.schedule_kind === 'cron' && { cron_expr: form.cron_expr.trim() }),
      ...(form.schedule_kind === 'interval' && {
        interval_seconds: parseInt(form.interval_seconds),
      }),
      ...(form.schedule_kind === 'once' && { run_at: form.run_at }),
      ...(form.delivery_channel && { delivery_channel_id: parseInt(form.delivery_channel) }),
      voice_call: form.voice_call,
    };

    // Task job: either existing sub-agent or automated sub-agent
    if (form.job_type === 'task') {
      if (form.sub_agent_mode === 'existing') {
        body.sub_agent_id = parseInt(form.sub_agent_id);
      } else {
        body.sub_agent_parameters = automatedSubAgentParameters(form);
      }
      // Always include prompt for task jobs (optional - backend will use default if empty)
      body.prompt = form.prompt.trim() || undefined;
    }

    // Watch job
    if (form.job_type === 'watch') {
      body.check_tool = form.check_tool;
      body.check_args = check_args;
      body.check_args_exprs =
        Object.keys(form.check_args_exprs).length > 0 ? form.check_args_exprs : undefined;
      body.destroy_after_trigger = form.destroy_after_trigger;
      // The two halves of the condition: the expression gates deterministically, the
      // judgement is the semantic stage on what it returned. At least one is set —
      // validated above and by the API.
      body.cel_expr = form.cel_expr.trim() || undefined;
      body.llm_condition = form.llm_condition.trim() || undefined;

      // The two outcomes are exclusive: a sub-agent's reply replaces the
      // notification, so sending both would leave one of them dead.
      if (form.outcome === 'agent') {
        if (form.sub_agent_mode === 'existing') {
          body.sub_agent_id = parseInt(form.sub_agent_id);
        } else {
          // An inline definition is as valid an outcome for a watch as for a task; it
          // used to be dropped here, silently downgrading the job to notify-only.
          body.sub_agent_parameters = automatedSubAgentParameters(form);
        }
        body.prompt = form.prompt.trim() || undefined;
      } else {
        body.notification_message = form.notification_message.trim();
      }
    }

    setSubmitting(true);
    try {
      const created = await createScheduledJob(body);
      qc.invalidateQueries({ queryKey: ['scheduler-jobs'] });
      onCreated(created.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] sm:max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Create Scheduled Job</DialogTitle>
          <DialogDescription>
            Configure a new job for the scheduler to run automatically.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 py-2">
          {/* Describe-the-job entry point. Above the fields it fills, because a box that
              writes into the form has to be read before the form, not after it — and
              shown for both job types, since it decides which one this is. */}
          <div className="bg-muted grid gap-2.5 rounded-md border p-3.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <Sparkles className="size-3.5" />
                <span className="text-[13px] font-semibold">Describe the job</span>
                <span className="text-muted-foreground text-xs">
                  fills every field below — review before saving
                </span>
              </div>
              <div className="flex gap-2">
                <Input
                  className="bg-background flex-1"
                  placeholder="e.g. every weekday morning, check whether my next meeting has attendees from outside the company and report to Slack"
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
              {aiFilled.size > 0 && (
                <div className="flex flex-wrap items-center gap-2 border-t pt-2.5">
                  <span className="flex items-center gap-1.5 text-xs">
                    <CheckCircle2 className="size-3.5" />
                    Filled {aiFilled.size} field{aiFilled.size === 1 ? '' : 's'}, each marked below.
                  </span>
                  {aiUndo && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="ml-auto"
                      onClick={undoAiFill}
                    >
                      <Undo2 className="size-3.5" /> Undo AI fill
                    </Button>
                  )}
                </div>
            )}
          </div>

          <SectionHeader n={1} title="What & when" />

          {/* Name */}
          <div className="grid gap-1.5">
            <Label htmlFor="name">
              Name
              {aiFilled.has('name') && <AiBadge />}
            </Label>
            <Input
              id="name"
              value={form.name}
              aria-invalid={Boolean(fieldErrors.name) || undefined}
              onChange={(e) => {
                update('name', e.target.value);
                clearAi('name');
                clearFieldError('name');
              }}
              placeholder="e.g. Daily report"
            />
            {fieldErrors.name && <FieldError>{fieldErrors.name}</FieldError>}
          </div>

          {/* Job type / Schedule kind */}
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-1.5">
              <Label>
                Job type
                {aiFilled.has('job_type') && <AiBadge />}
              </Label>
              <Select
                value={form.job_type}
                onValueChange={(v) => {
                  // Reset cross-type fields so a task-mode selection can't
                  // silently carry over into a watch (and vice versa).
                  setForm((f) => ({
                    ...f,
                    job_type: v as JobType,
                    sub_agent_id: '',
                    prompt: '',
                  }));
                  clearAi('job_type');
                }}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="task">Task – run agent</SelectItem>
                  <SelectItem value="watch">Watch – poll condition, then notify or run agent</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5">
              <Label>
                Schedule
                {aiFilled.has('schedule') && <AiBadge />}
              </Label>
              <Select
                value={form.schedule_kind}
                onValueChange={(v) => {
                  update('schedule_kind', v as ScheduleKind);
                  clearAi('schedule');
                }}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="cron">Cron expression</SelectItem>
                  <SelectItem value="interval">Fixed interval</SelectItem>
                  <SelectItem value="once">Run once</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {/* Schedule detail */}
          {form.schedule_kind === 'cron' && (
            <CronField
              id="cron"
              value={form.cron_expr}
              onChange={(v) => update('cron_expr', v)}
              timezone={userTimezone}
            />
          )}
          {form.schedule_kind === 'interval' && (
            <div className="grid gap-1.5">
              <Label htmlFor="interval">Interval (seconds, min 60)</Label>
              <Input
                id="interval"
                type="number"
                min={60}
                value={form.interval_seconds}
                onChange={(e) => update('interval_seconds', e.target.value)}
                placeholder="3600"
              />
            </div>
          )}
          {form.schedule_kind === 'once' && (
            <div className="grid gap-1.5">
              <Label htmlFor="run_at">Run at</Label>
              <Input
                id="run_at"
                type="datetime-local"
                min={nowDatetimeLocal(userTimezone)}
                value={form.run_at}
                onChange={(e) => update('run_at', e.target.value)}
              />
              {userTimezone && (
                <p className="text-xs text-muted-foreground">
                  Interpreted in {userTimezone}
                </p>
              )}
            </div>
          )}

          {/* What to run. The same panel serves the watch job's sub-agent outcome:
              the trigger differs, what runs does not. */}
          {form.job_type === 'task' && (
            <>
              <SectionHeader n={2} title="What to run" />
              <AgentActionFields
                value={form}
                onChange={(patch) => {
                  setForm((f) => ({ ...f, ...patch }));
                  setError(null);
                }}
                subAgents={subAgents}
                mcpTools={mcpTools}
                instructionLabel="Prompt"
                instructionPlaceholder="Specific task or instruction for this execution (leave empty for default behavior)…"
                instructionHint={
                  form.sub_agent_mode === 'automated'
                    ? 'If empty, the agent follows its configured system prompt.'
                    : 'Sent to the sub-agent. If empty, defaults to "Execute your configured task."'
                }
                onLimitExceeded={setError}
                fieldErrors={fieldErrors}
              />
            </>
          )}

          {/* Watch fields — the same component the job detail page renders, so a field
              cannot be added to one and forgotten in the other. */}
          {form.job_type === 'watch' && (
            <WatchFields
              mode="edit"
              value={form}
              onChange={(next) => {
                setForm((f) => ({ ...f, ...next }));
                // Editing a field is what clears its AI marker and its error: the page
                // knows which keys changed, so neither has to be cleared field by field.
                clearAi(...Object.keys(next));
                clearFieldError(...Object.keys(next));
                setError(null);
              }}
              mcpTools={mcpTools}
              subAgents={subAgents}
              fieldErrors={fieldErrors}
              aiFilled={aiFilled}
              onError={setError}
            />
          )}

          {/* How the outcome reaches the user. Voice belongs here rather than with the
              agent config: it decides how the user hears about it, and it applies to a
              watch that only notifies just as much as to one that runs an agent. */}
          <SectionHeader n={form.job_type === 'watch' ? 5 : 4} title="How to reach you" />

          <div className="flex items-center justify-between rounded-lg border p-3">
            <div className="space-y-0.5">
              <Label htmlFor="voice_call" className="cursor-pointer">
                Phone call
              </Label>
              <p className="text-muted-foreground text-xs">
                {form.job_type === 'watch'
                  ? 'Call instead of sending a message, when the condition is met.'
                  : 'Deliver this job as a phone call via the voice agent.'}
              </p>
            </div>
            <Switch
              id="voice_call"
              checked={form.voice_call}
              onCheckedChange={(checked) => update('voice_call', checked)}
            />
          </div>

          <div className="grid gap-1.5">
            <Label>
              Delivery channel <span className="text-muted-foreground text-xs">(optional)</span>
              {aiFilled.has('delivery_channel') && <AiBadge />}
            </Label>
            <Select
              value={form.delivery_channel || '_none'}
              onValueChange={(v) => {
                update('delivery_channel', v === '_none' ? '' : v);
                clearAi('delivery_channel');
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder="None (in-app notifications only)" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="_none">
                  <span className="text-muted-foreground">None (in-app only)</span>
                </SelectItem>
                {channels.map((ch) => (
                  <SelectItem key={ch.id} value={String(ch.id)}>
                    {ch.name}
                    {ch.description && (
                      <span className="ml-2 text-xs text-muted-foreground">
                        — {ch.description}
                      </span>
                    )}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={submitting}>
            {submitting ? 'Creating…' : 'Create job'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function SchedulerPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [showCreate, setShowCreate] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ScheduledJob | null>(null);

  const { data: jobs = [], isLoading } = useQuery({
    queryKey: ['scheduler-jobs'],
    queryFn: listJobs,
  });

  const pauseMutation = useMutation({
    mutationFn: (jobId: number) => pauseJob(jobId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['scheduler-jobs'] }),
  });

  const resumeMutation = useMutation({
    mutationFn: (jobId: number) => resumeJob(jobId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['scheduler-jobs'] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (jobId: number) => deleteJob(jobId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['scheduler-jobs'] }),
  });

  return (
    <div className="flex flex-col gap-6 p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Scheduler</h1>
          <p className="text-muted-foreground">
            Create and manage automated scheduled jobs
          </p>
        </div>
        <Button onClick={() => setShowCreate(true)}>
          <Plus className="mr-2 h-4 w-4" />
          New Job
        </Button>
      </div>

      {/* Job table */}
      {isLoading ? (
        <TableSkeleton columns={7} />
      ) : jobs.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-12 text-center">
          <Calendar className="h-8 w-8 text-muted-foreground" />
          <div>
            <p className="font-medium">No scheduled jobs yet</p>
            <p className="text-muted-foreground text-sm">
              Click "New Job" to create your first scheduled job
            </p>
          </div>
        </div>
      ) : (
        <div className="rounded-lg border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="px-4 py-3 text-left font-medium">Name</th>
                <th className="px-4 py-3 text-left font-medium">Type</th>
                <th className="px-4 py-3 text-left font-medium">Schedule</th>
                <th className="px-4 py-3 text-left font-medium">Status</th>
                <th className="px-4 py-3 text-left font-medium">Next run</th>
                <th className="px-4 py-3 text-left font-medium">Last run</th>
                <th className="px-4 py-3 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr
                  key={job.id}
                  className="border-b last:border-0 hover:bg-muted/30 cursor-pointer"
                  onClick={() => navigate(`/app/scheduler/${job.id}`)}
                >
                  <td className="px-4 py-3 font-medium">
                    <div className="flex items-center gap-2">
                      {job.name}
                      <ChevronRight className="h-3 w-3 text-muted-foreground" />
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant="outline" className="capitalize">
                      {job.job_type}
                    </Badge>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                    {scheduleLabel(job)}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge job={job} />
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    <span className="flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      {formatDateRelative(job.next_run_at)}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatDateRelative(job.last_run_at)}
                  </td>
                  <td className="px-4 py-3">
                    <div
                      className="flex justify-end gap-1"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {job.enabled ? (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={pauseMutation.isPending}
                              onClick={() => pauseMutation.mutate(job.id)}
                            >
                              <Pause className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>Pause</TooltipContent>
                        </Tooltip>
                      ) : (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={resumeMutation.isPending}
                              onClick={() => resumeMutation.mutate(job.id)}
                            >
                              <Play className="h-4 w-4" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>Resume</TooltipContent>
                        </Tooltip>
                      )}
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="text-destructive hover:text-destructive"
                            disabled={deleteMutation.isPending}
                            onClick={() => setDeleteTarget(job)}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Delete</TooltipContent>
                      </Tooltip>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Dialogs */}
      <CreateJobDialog
        open={showCreate}
        onClose={() => setShowCreate(false)}
        onCreated={(jobId) => {
          setShowCreate(false);
          navigate(`/app/scheduler/${jobId}`);
        }}
      />

      {/* Delete confirmation */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(o) => { if (!o) setDeleteTarget(null); }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete scheduled job?</AlertDialogTitle>
            <AlertDialogDescription>
              The job <strong>{deleteTarget?.name}</strong> will be permanently deleted.
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteMutation.isPending}
              onClick={() => {
                if (deleteTarget) {
                  deleteMutation.mutate(deleteTarget.id);
                  setDeleteTarget(null);
                }
              }}
            >
              {deleteMutation.isPending ? 'Deleting…' : (
                <>
                  <Trash2 className="mr-1.5 h-4 w-4" />
                  Delete
                </>
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
