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
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { config } from '@/config';
import {
  type JobRunStatus,
  type ScheduledJob,
  type ScheduledJobRun,
  getDeliveryChannels,
  generateWatchParams,
  updateScheduledJob,
  runJobNow,
  type DeliveryChannel,
  getJob,
  listRuns,
  pauseJob,
  resumeJob,
  deleteJob,
} from '@/api/scheduler';
import {
  playgroundListSubAgentsOptions,
  playgroundListMcpToolsOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import { useAuth } from '@/contexts/AuthContext';
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

function formatDuration(
  start: string | null | undefined,
  end: string | null | undefined,
): string {
  if (!start || !end) return '—';
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}

function scheduleLabel(job: ScheduledJob): string {
  if (job.schedule_kind === 'cron') return job.cron_expr ?? '—';
  if (job.schedule_kind === 'interval')
    return job.interval_seconds ? `every ${job.interval_seconds}s` : '—';
  if (job.schedule_kind === 'once' && job.run_at)
    return new Date(job.run_at).toLocaleString();
  return '—';
}

/** Date-time string in YYYY-MM-DDTHH:mm format suitable for datetime-local input, clamped to "now". */
function nowDatetimeLocal(): string {
  const d = new Date();
  d.setSeconds(0, 0);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

/** Convert an ISO string from the backend into YYYY-MM-DDTHH:mm for datetime-local input. */
function toDatetimeLocal(iso: string): string {
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
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
          <span>·</span>
          {job.enabled ? (
            <span className="flex items-center gap-1 text-green-600">
              <CheckCircle2 className="h-3.5 w-3.5" /> Active
            </span>
          ) : (
            <span className="flex items-center gap-1 text-muted-foreground">
              <Pause className="h-3.5 w-3.5" /> Paused
              {job.paused_reason && (
                <span className="text-xs">({job.paused_reason})</span>
              )}
            </span>
          )}
        </div>
      </div>

      <div className="flex gap-2">
        <Button
          variant="default"
          size="sm"
          disabled={isRunningNow}
          onClick={onRunNow}
          title="Trigger a full test run right now — resolves token, calls agent-runner, delivers webhook"
        >
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
        {job.enabled ? (
          <Button
            variant="outline"
            size="sm"
            disabled={isPendingPause}
            onClick={onPause}
          >
            <Pause className="mr-1.5 h-4 w-4" />
            Pause
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            disabled={isPendingResume}
            onClick={onResume}
          >
            <Play className="mr-1.5 h-4 w-4" />
            Resume
          </Button>
        )}
        <Button
          variant="destructive"
          size="sm"
          className="ml-2"
          disabled={isPendingDelete}
          onClick={onDelete}
        >
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

function EditForm({ job }: { job: ScheduledJob }) {
  const qc = useQueryClient();

  // ── Per-field state ───────────────────────────────────────────────────────
  const [name, setName] = useState(job.name ?? '');
  const [maxFailures, setMaxFailures] = useState(job.max_failures ?? 3);
  const [cronExpr, setCronExpr] = useState(job.cron_expr ?? '');
  const [intervalSeconds, setIntervalSeconds] = useState(
    job.interval_seconds != null ? String(job.interval_seconds) : '',
  );
  const [runAt, setRunAt] = useState(
    job.run_at ? toDatetimeLocal(job.run_at) : '',
  );
  const [message, setMessage] = useState(
    job.job_type === 'task' ? (job.prompt ?? '') : (job.notification_message ?? '')
  );
  const [subAgentId, setSubAgentId] = useState(
    job.sub_agent_id != null ? String(job.sub_agent_id) : '',
  );
  const [checkTool, setCheckTool] = useState(job.check_tool ?? '');
  const [checkArgsText, setCheckArgsText] = useState(
    job.check_args ? JSON.stringify(job.check_args, null, 2) : '',
  );
  const [conditionExpr, setConditionExpr] = useState(job.condition_expr ?? '');
  const [expectedValue, setExpectedValue] = useState(job.expected_value ?? '');
  const [deliveryChannel, setDeliveryChannel] = useState(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (job as any).delivery_channel_id != null ? String((job as any).delivery_channel_id) : '',
  );
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [voiceCall, setVoiceCall] = useState((job as any).voice_call ?? false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [aiQuery, setAiQuery] = useState('');
  const [aiLoading, setAiLoading] = useState(false);

  // ── Data queries ──────────────────────────────────────────────────────────
  const { data: subAgentsData } = useQuery(
    playgroundListSubAgentsOptions({ query: { owned_only: true } }),
  );
  const subAgents = subAgentsData?.items ?? [];

  const { data: mcpToolsData } = useQuery(playgroundListMcpToolsOptions());
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

  const selectedTool = mcpTools.find((t) => t.name === checkTool);

  function touch() {
    setDirty(true);
    setError(null);
  }

  function resetForm() {
    setName(job.name ?? '');
    setMaxFailures(job.max_failures ?? 3);
    setCronExpr(job.cron_expr ?? '');
    setIntervalSeconds(job.interval_seconds != null ? String(job.interval_seconds) : '');
    setRunAt(job.run_at ? toDatetimeLocal(job.run_at) : '');
    setMessage(job.job_type === 'task' ? (job.prompt ?? '') : (job.notification_message ?? ''));
    setSubAgentId(job.sub_agent_id != null ? String(job.sub_agent_id) : '');
    setCheckTool(job.check_tool ?? '');
    setCheckArgsText(job.check_args ? JSON.stringify(job.check_args, null, 2) : '');
    setConditionExpr(job.condition_expr ?? '');
    setExpectedValue(job.expected_value ?? '');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    setDeliveryChannel((job as any).delivery_channel_id != null ? String((job as any).delivery_channel_id) : '');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    setVoiceCall((job as any).voice_call ?? false);
    setDirty(false);
    setError(null);
  }

  // ── AI generation (watch jobs only) ──────────────────────────────────────
  async function handleAiGenerate() {
    if (!aiQuery.trim()) return;
    setAiLoading(true);
    setError(null);
    try {
      const result = await generateWatchParams(
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        mcpTools as unknown as Record<string, unknown>[],
        aiQuery,
      );
      if (result.check_tool) { setCheckTool(result.check_tool); touch(); }
      if (result.check_args) { setCheckArgsText(JSON.stringify(result.check_args, null, 2)); touch(); }
      if (result.condition_expr) { setConditionExpr(result.condition_expr); touch(); }
      if (result.expected_value) { setExpectedValue(result.expected_value); touch(); }
      if (result.notification_message) { setMessage(result.notification_message); touch(); }
    } catch {
      setError('AI generation failed. Please fill in the fields manually.');
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
    },
    onError: (e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    },
  });

  function handleSave() {
    let check_args: Record<string, unknown> | undefined;
    if (checkArgsText.trim()) {
      try {
        check_args = JSON.parse(checkArgsText);
      } catch {
        setError('Check arguments must be valid JSON');
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
      ...(job.schedule_kind === 'once' && { run_at: runAt || undefined }),
      ...(job.job_type === 'task' && {
        sub_agent_id: subAgentId ? parseInt(subAgentId) : undefined,
        prompt: message.trim() ? message.trim() : null, // Task jobs use prompt field
      }),
      ...(job.job_type === 'watch' && {
        notification_message: message.trim() ? message.trim() : null, // Watch jobs use notification_message field
        check_tool: checkTool || undefined,
        check_args,
        condition_expr: conditionExpr || undefined,
        expected_value: expectedValue || undefined,
      }),
      ...(deliveryChannel && { delivery_channel_id: parseInt(deliveryChannel) }),
      voice_call: voiceCall,
    };

    mutation.mutate(body);
  }

  return (
    <div className="grid gap-4">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold">Job configuration</h2>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={resetForm}
            disabled={!dirty || mutation.isPending}
          >
            <Undo2 className="mr-1.5 h-4 w-4" />
            Discard
          </Button>
          <Button
            size="sm"
            onClick={handleSave}
            disabled={!dirty || mutation.isPending}
          >
            <Save className="mr-1.5 h-4 w-4" />
            {mutation.isPending ? 'Saving…' : 'Save changes'}
          </Button>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="grid gap-1.5">
          <Label>Name</Label>
          <Input
            value={name}
            onChange={(e) => { setName(e.target.value); touch(); }}
          />
        </div>
        <div className="grid gap-1.5">
          <Label>Max failures before pause</Label>
          <Input
            type="number"
            min={1}
            max={20}
            value={maxFailures}
            onChange={(e) => { setMaxFailures(parseInt(e.target.value) || 3); touch(); }}
          />
        </div>
      </div>

      {job.schedule_kind === 'cron' && (
        <div className="grid gap-1.5">
          <Label>
            Cron expression{' '}
            <span className="text-muted-foreground text-xs">(e.g. 0 9 * * 1-5)</span>
          </Label>
          <Input
            value={cronExpr}
            onChange={(e) => { setCronExpr(e.target.value); touch(); }}
          />
        </div>
      )}

      {job.schedule_kind === 'interval' && (
        <div className="grid gap-1.5">
          <Label>Interval (seconds)</Label>
          <Input
            type="number"
            min={60}
            value={intervalSeconds}
            onChange={(e) => { setIntervalSeconds(e.target.value); touch(); }}
          />
        </div>
      )}

      {job.schedule_kind === 'once' && (
        <div className="grid gap-1.5">
          <Label>Run at</Label>
          <Input
            type="datetime-local"
            min={nowDatetimeLocal()}
            value={runAt}
            onChange={(e) => { setRunAt(e.target.value); touch(); }}
          />
        </div>
      )}

      {/* Sub-agent picker (task jobs) */}
      {job.job_type === 'task' && (
        <>
          <div className="grid gap-1.5">
            <Label>Sub-agent</Label>
            <Select
              value={subAgentId}
              onValueChange={(v) => { setSubAgentId(v); touch(); }}
            >
              <SelectTrigger>
                <SelectValue placeholder="Select a sub-agent…" />
              </SelectTrigger>
              <SelectContent>
                {subAgents.length === 0 ? (
                  <div className="px-3 py-2 text-sm text-muted-foreground">
                    No sub-agents found
                  </div>
                ) : (
                  subAgents.filter((sa) => sa.name !== 'voice-agent').map((sa) => (
                    <SelectItem key={sa.id} value={String(sa.id)}>
                      <span>{sa.name}</span>
                      {sa.type === 'automated' && (
                        <span className="ml-2 text-xs text-muted-foreground">(automated)</span>
                      )}
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              {subAgents.find(sa => sa.id === parseInt(subAgentId))?.type === 'automated' 
                ? 'This automated sub-agent has a predefined system prompt.'
                : 'Select a sub-agent to execute for this scheduled job.'}
            </p>
          </div>

          {/* Task instruction - always shown for task jobs */}
          <div className="grid gap-1.5">
            <Label>
              Task instruction{' '}
              <span className="text-muted-foreground text-xs">(optional)</span>
            </Label>
            <Textarea
              rows={3}
              value={message}
              onChange={(e) => { setMessage(e.target.value); touch(); }}
              placeholder="Specific task or instruction for this execution (leave empty for default behavior)…"
            />
            <p className="text-xs text-muted-foreground">
              {subAgents.find(sa => sa.id === parseInt(subAgentId))?.type === 'automated'
                ? 'Optional task-specific instruction. If empty, the agent will follow its configured system prompt.'
                : 'This instruction will be sent to the sub-agent. If empty, defaults to "Execute your configured task."'}
            </p>
          </div>
        </>
      )}

      {/* Watch-specific fields */}
      {job.job_type === 'watch' && (
        <>
          {/* AI generate */}
          <div className="grid gap-2 rounded-lg border border-dashed px-3 py-3">
            <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Sparkles className="h-3.5 w-3.5" />
              AI-fill all watch fields
            </p>
            <div className="flex gap-2">
              <Input
                placeholder="Describe the condition to watch (e.g. 'price drops below 100')"
                value={aiQuery}
                onChange={(e) => setAiQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleAiGenerate()}
                className="flex-1 text-sm"
              />
              <Button
                type="button"
                size="sm"
                variant="secondary"
                disabled={!aiQuery.trim() || aiLoading || mcpTools.length === 0}
                onClick={handleAiGenerate}
              >
                {aiLoading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  'Generate'
                )}
              </Button>
            </div>
          </div>

          {/* Check tool */}
          <div className="grid gap-1.5">
            <Label>Check tool</Label>
            <Select
              value={checkTool}
              onValueChange={(v) => { setCheckTool(v); touch(); }}
            >
              <SelectTrigger>
                <SelectValue placeholder="Select an MCP tool…" />
              </SelectTrigger>
              <SelectContent>
                {mcpTools.length === 0 ? (
                  <div className="px-3 py-2 text-sm text-muted-foreground">
                    No MCP tools available
                  </div>
                ) : (
                  mcpTools.map((tool) => (
                    <SelectItem key={tool.name} value={tool.name}>
                      <span>{tool.name}</span>
                      {tool.server && (
                        <span className="ml-2 text-xs text-muted-foreground">
                          ({tool.server})
                        </span>
                      )}
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
            {selectedTool?.description && (
              <p className="text-xs text-muted-foreground">{selectedTool.description}</p>
            )}
          </div>

          {/* Check arguments */}
          <div className="grid gap-1.5">
            <Label>
              Check arguments{' '}
              <span className="text-muted-foreground text-xs">(JSON, optional)</span>
            </Label>
            <Textarea
              rows={2}
              value={checkArgsText}
              onChange={(e) => { setCheckArgsText(e.target.value); touch(); }}
              placeholder='{"key": "value"}'
              className="font-mono text-xs"
            />
          </div>

          {/* Condition expression */}
          <div className="grid gap-1.5">
            <Label>
              Condition expression{' '}
              <span className="text-muted-foreground text-xs">(JSONPath)</span>
            </Label>
            <Input
              value={conditionExpr}
              onChange={(e) => { setConditionExpr(e.target.value); touch(); }}
              placeholder="$.status"
            />
          </div>

          {/* Expected value */}
          <div className="grid gap-1.5">
            <Label>
              Expected value{' '}
              <span className="text-muted-foreground text-xs">(leave empty to check "is not null")</span>
            </Label>
            <Input
              value={expectedValue}
              onChange={(e) => { setExpectedValue(e.target.value); touch(); }}
              placeholder="success"
            />
          </div>

        </>
      )}

      {/* Notification text for watch jobs */}
      {job.job_type === 'watch' && (
        <div className="grid gap-1.5">
          <Label>Notification text</Label>
          <Textarea
            rows={3}
            value={message}
            onChange={(e) => { setMessage(e.target.value); touch(); }}
            placeholder="Message sent when the condition is met."
          />
          <p className="text-xs text-muted-foreground">
            Custom notification message delivered when the condition is met. If left empty, an LLM will generate a message based on the check result.
          </p>
        </div>
      )}

      {/* Voice call toggle (task jobs) */}
      {job.job_type === 'task' && (
        <div className="flex items-center gap-3 rounded-lg border px-3 py-2">
          <Switch
            id="voice-call-edit"
            checked={voiceCall}
            onCheckedChange={(v) => { setVoiceCall(v); touch(); }}
          />
          <Label htmlFor="voice-call-edit" className="cursor-pointer text-sm">
            Deliver via voice call
          </Label>
          <span className="text-xs text-muted-foreground">
            When enabled, the agent response is delivered as a phone call instead of a text message.
          </span>
        </div>
      )}

      {/* Delivery channel */}
      <div className="grid gap-1.5">
        <Label>Delivery channel</Label>
        <Select
          value={deliveryChannel}
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
              <div className="px-3 py-2 text-sm text-muted-foreground">
                No delivery channels registered
              </div>
            ) : (
              channels.map((ch) => (
                <SelectItem key={ch.id} value={String(ch.id)}>
                  {ch.name}
                  {ch.description && (
                    <span className="ml-2 text-xs text-muted-foreground">
                      — {ch.description}
                    </span>
                  )}
                </SelectItem>
              ))
            )}
          </SelectContent>
        </Select>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {/* Read-only info */}
      <div className="grid gap-2 rounded-lg bg-muted/30 p-3 text-sm text-muted-foreground sm:grid-cols-2">
        <div>
          <span className="font-medium text-foreground">Created:</span>{' '}
          {formatDate(job.created_at)}
        </div>
        <div>
          <span className="font-medium text-foreground">Last updated:</span>{' '}
          {formatDate(job.updated_at)}
        </div>
        <div>
          <span className="font-medium text-foreground">Next run:</span>{' '}
          {formatDate(job.next_run_at)}
        </div>
        <div>
          <span className="font-medium text-foreground">Consecutive failures:</span>{' '}
          {job.consecutive_failures}
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
              <td className="px-4 py-3 text-muted-foreground">
                {formatDate(run.started_at)}
              </td>
              <td className="px-4 py-3 text-muted-foreground">
                {formatDuration(run.started_at, run.completed_at)}
              </td>
              <td className="px-4 py-3">
                <RunStatusBadge status={run.status} />
              </td>
              <td className="px-4 py-3 max-w-xs">
                {run.status === 'failed' && run.error_message ? (
                  <span className="text-destructive text-xs line-clamp-2">
                    {run.error_message}
                  </span>
                ) : (
                  <span className="text-muted-foreground text-xs line-clamp-2">
                    {run.result_summary ?? '—'}
                  </span>
                )}
              </td>
              <td className="px-4 py-3 text-center">
                {run.delivered ? (
                  <div title="Notification sent (best effort — delivery receipt not confirmed)">
                    <Send className="mx-auto h-4 w-4 text-muted-foreground" />
                  </div>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
              <td className="px-4 py-3 text-center">
                {run.conversation_id ? (
                  <a
                    href={`/app/usage?conversation_id=${run.conversation_id}`}
                    className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                    title="View usage logs for this run"
                  >
                    <ExternalLink className="h-3 w-3" />
                  </a>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
              {isAdmin && (
                <td className="px-4 py-3 text-center">
                  {run.conversation_id ? (
                    <a
                      href={`https://eu.smith.langchain.com/o/${config.langsmith.organizationId}/projects/p/${config.langsmith.projectId}/t/${run.conversation_id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                      title="View trace in LangSmith"
                    >
                      <ExternalLink className="h-3 w-3" />
                    </a>
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
    return () => { socket.disconnect(); };
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
    if (job && confirm(`Delete job "${job.name}"? This cannot be undone.`)) {
      deleteMutation.mutate();
    }
  }

  return (
    <div className="flex flex-col gap-6 p-4">
      {/* Back button */}
      <Button
        variant="ghost"
        size="sm"
        className="-ml-1 w-fit"
        onClick={() => navigate('/app/scheduler')}
      >
        <ArrowLeft className="mr-1.5 h-4 w-4" />
        Back to Scheduler
      </Button>

      {jobLoading && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading…
        </div>
      )}

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
                <p><strong>Run failed:</strong> {runNowError}</p>
              ) : runNowResult && (
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <RunStatusBadge status={runNowResult.status} />
                    {runNowResult.result_summary && (
                      <span className="text-xs">{runNowResult.result_summary}</span>
                    )}
                    {runNowResult.error_message && (
                      <span className="text-xs">{runNowResult.error_message}</span>
                    )}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {runNowResult.delivered ? '↗ Webhook notified (best effort)' : '○ No webhook configured'}
                  </span>
                </div>
              )}
            </div>
          )}

          <Separator />

          <EditForm job={job} />

          <Separator />

          <div className="flex flex-col gap-3">
            <h2 className="font-semibold">
              Run history
              {runsLoading && (
                <Loader2 className="ml-2 inline h-4 w-4 animate-spin text-muted-foreground" />
              )}
            </h2>
            <RunHistoryTable runs={runs} />
          </div>
        </>
      )}
    </div>
  );
}
