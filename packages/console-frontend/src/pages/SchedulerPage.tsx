import { useState, useEffect } from 'react';
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
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
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
import {
  type ScheduledJob,
  type JobType,
  type ScheduleKind,
  type ScheduledJobCreateExtended,
  getDeliveryChannels,
  generateWatchParams,
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
} from '@/api/generated/@tanstack/react-query.gen';
import { config } from '@/config';

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

/** Date-time string in YYYY-MM-DDTHH:mm format suitable for datetime-local input, clamped to "now". */
function nowDatetimeLocal(): string {
  const d = new Date();
  d.setSeconds(0, 0);
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
  check_args_text: string;
  condition_expr: string;
  expected_value: string;
  llm_condition: string;
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
  automated_model: 'claude-sonnet-4.5',
  automated_system_prompt: '',
  automated_mcp_tools: [],
  automated_enable_thinking: false,
  automated_thinking_level: 'low',
  prompt: '',
  notification_message: '',
  check_tool: '',
  check_args_text: '',
  condition_expr: '',
  expected_value: '',
  llm_condition: '',
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

  const qc = useQueryClient();

  // ── Data queries ──────────────────────────────────────────────────────────
  const { data: subAgentsData } = useQuery(
    consoleListSubAgentsOptions({ query: { owned_only: true } }),
  );
  const subAgents = subAgentsData?.items ?? [];

  const { data: mcpToolsData } = useQuery(consoleListMcpToolsOptions());
  const mcpTools = mcpToolsData?.tools ?? [];

  const { data: channels = [] } = useQuery<DeliveryChannel[]>({
    queryKey: ['delivery-channels'],
    queryFn: getDeliveryChannels,
    staleTime: 60_000,
  });

  // Pre-select the first channel once loaded
  useEffect(() => {
  }, [channels]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedTool = mcpTools.find((t) => t.name === form.check_tool);

  // ── Helpers ───────────────────────────────────────────────────────────────
  function update<K extends keyof CreateJobForm>(key: K, value: CreateJobForm[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    setError(null);
  }

  // ── AI generation ─────────────────────────────────────────────────────────
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
      if (result.check_tool) update('check_tool', result.check_tool);
      if (result.check_args) {
        update('check_args_text', JSON.stringify(result.check_args, null, 2));
      }
      if (result.condition_expr) update('condition_expr', result.condition_expr);
      if (result.expected_value) update('expected_value', result.expected_value);
      if (result.llm_condition) update('llm_condition', result.llm_condition);
      if (result.notification_message) update('notification_message', result.notification_message);
    } catch {
      setError('AI generation failed. Please fill in the fields manually.');
    } finally {
      setAiLoading(false);
    }
  }

  // ── Submission ────────────────────────────────────────────────────────────
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    if (!form.name.trim()) return setError('Name is required');
    if (form.schedule_kind === 'cron' && !form.cron_expr.trim())
      return setError('Cron expression is required');
    if (form.schedule_kind === 'interval' && !form.interval_seconds)
      return setError('Interval is required');
    if (form.schedule_kind === 'once' && !form.run_at)
      return setError('Date/time is required');
    
    // Task job validations
    if (form.job_type === 'task') {
      if (form.sub_agent_mode === 'existing') {
        if (!form.sub_agent_id) return setError('Sub-agent is required');
      } else {
        // Automated sub-agent validations
        if (!form.automated_name.trim()) return setError('Sub-agent name is required');
        if (!form.automated_description.trim()) return setError('Sub-agent description is required');
        if (!form.automated_model) return setError('Model is required');
        if (!form.automated_system_prompt.trim()) return setError('System prompt is required');
        if (form.automated_system_prompt.length > config.autoApprove.maxSystemPromptLength) return setError(`System prompt must be ${config.autoApprove.maxSystemPromptLength} characters or less`);
        if (form.automated_description.length > 200) return setError('Description must be 200 characters or less');
        if (form.automated_mcp_tools.length > config.autoApprove.maxMcpToolsCount) return setError(`Maximum ${config.autoApprove.maxMcpToolsCount} MCP tools allowed`);
      }
      // Message is optional for all task jobs
    }
    
    // Watch job validations
    if (form.job_type === 'watch') {
      if (!form.check_tool) return setError('Check tool is required for watch jobs');
      if (!form.condition_expr.trim()) return setError('Condition expression is required for watch jobs');
      // Message is optional - LLM will generate one if left empty
    }

    // Parse check_args JSON
    let check_args: Record<string, unknown> | undefined;
    if (form.check_args_text.trim()) {
      try {
        check_args = JSON.parse(form.check_args_text);
      } catch {
        return setError('Check arguments must be valid JSON');
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
        // Automated sub-agent configuration
        body.sub_agent_parameters = {
          name: form.automated_name.trim(),
          description: form.automated_description.trim(),
          model: form.automated_model,
          system_prompt: form.automated_system_prompt.trim(),
          mcp_tools: form.automated_mcp_tools.length > 0 ? form.automated_mcp_tools : null,
          enable_thinking: form.automated_enable_thinking || null,
          thinking_level: form.automated_enable_thinking ? form.automated_thinking_level : null,
        };
      }
      // Always include prompt for task jobs (optional - backend will use default if empty)
      body.prompt = form.prompt.trim() || undefined;
    }

    // Watch job
    if (form.job_type === 'watch') {
      body.check_tool = form.check_tool;
      body.check_args = check_args;
      body.condition_expr = form.condition_expr.trim();
      body.expected_value = form.expected_value.trim() || undefined;
      body.llm_condition = form.llm_condition.trim() || undefined;
      body.destroy_after_trigger = form.destroy_after_trigger;
      body.notification_message = form.notification_message.trim();
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
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Create Scheduled Job</DialogTitle>
          <DialogDescription>
            Configure a new job for the scheduler to run automatically.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 py-2">
          {/* Name */}
          <div className="grid gap-1.5">
            <Label htmlFor="name">Name</Label>
            <Input
              id="name"
              value={form.name}
              onChange={(e) => update('name', e.target.value)}
              placeholder="e.g. Daily report"
            />
          </div>

          {/* Job type / Schedule kind */}
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-1.5">
              <Label>Job type</Label>
              <Select
                value={form.job_type}
                onValueChange={(v) => update('job_type', v as JobType)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="task">Task – run agent</SelectItem>
                  <SelectItem value="watch">Watch – poll condition then notify</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5">
              <Label>Schedule</Label>
              <Select
                value={form.schedule_kind}
                onValueChange={(v) => update('schedule_kind', v as ScheduleKind)}
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
            <div className="grid gap-1.5">
              <Label htmlFor="cron">
                Cron expression{' '}
                <span className="text-muted-foreground text-xs">(e.g. 0 9 * * 1-5)</span>
              </Label>
              <Input
                id="cron"
                value={form.cron_expr}
                onChange={(e) => update('cron_expr', e.target.value)}
                placeholder="0 9 * * 1-5"
              />
            </div>
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
                min={nowDatetimeLocal()}
                value={form.run_at}
                onChange={(e) => update('run_at', e.target.value)}
              />
            </div>
          )}

          {/* Task job configuration */}
          {form.job_type === 'task' && (
            <>
              {/* Sub-agent mode selector */}
              <div className="grid gap-1.5">
                <Label>Sub-agent configuration</Label>
                <Select
                  value={form.sub_agent_mode}
                  onValueChange={(v) => update('sub_agent_mode', v as SubAgentMode)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="existing">Use existing sub-agent</SelectItem>
                    <SelectItem value="automated">Create automated sub-agent</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {/* Existing sub-agent mode */}
              {form.sub_agent_mode === 'existing' && (
                <>
                  <div className="grid gap-1.5">
                    <Label>Sub-agent</Label>
                    <Select
                      value={form.sub_agent_id}
                      onValueChange={(v) => update('sub_agent_id', v)}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Select a sub-agent…" />
                      </SelectTrigger>
                      <SelectContent>
                        {subAgents.filter((sa) => sa.name !== 'voice-agent').length === 0 ? (
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
                  </div>
                </>
              )}

              {/* Automated sub-agent mode */}
              {form.sub_agent_mode === 'automated' && (
                <>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="grid gap-1.5">
                      <Label htmlFor="automated_name">Sub-agent name</Label>
                      <Input
                        id="automated_name"
                        value={form.automated_name}
                        onChange={(e) => update('automated_name', e.target.value)}
                        placeholder="My Automated Agent"
                      />
                    </div>
                    <div className="grid gap-1.5">
                      <Label htmlFor="automated_model">Model</Label>
                      <Select
                        value={form.automated_model}
                        onValueChange={(v) => update('automated_model', v)}
                      >
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="claude-sonnet-4.5">Claude Sonnet 4.5</SelectItem>
                          <SelectItem value="claude-sonnet-4.6">Claude Sonnet 4.6</SelectItem>
                          <SelectItem value="claude-haiku-4-5">Claude Haiku 4</SelectItem>
                          <SelectItem value="gpt-4o">GPT-4o</SelectItem>
                          <SelectItem value="gpt-4o-mini">GPT-4o Mini</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  </div>

                  <div className="grid gap-1.5">
                    <Label htmlFor="automated_description">
                      Description{' '}
                      <span className="text-muted-foreground text-xs">(max 200 chars)</span>
                    </Label>
                    <Input
                      id="automated_description"
                      value={form.automated_description}
                      onChange={(e) => update('automated_description', e.target.value)}
                      placeholder="Short description of the agent's skill"
                      maxLength={200}
                    />
                  </div>

                  <div className="grid gap-1.5">
                    <Label htmlFor="automated_system_prompt">
                      System prompt{' '}
                      <span className="text-muted-foreground text-xs">(max {config.autoApprove.maxSystemPromptLength} chars)</span>
                    </Label>
                    <Textarea
                      id="automated_system_prompt"
                      rows={4}
                      value={form.automated_system_prompt}
                      onChange={(e) => update('automated_system_prompt', e.target.value)}
                      placeholder="System prompt describing the task for the agent…"
                      maxLength={config.autoApprove.maxSystemPromptLength}
                    />
                  </div>

                  <div className="grid gap-1.5">
                    <Label>
                      MCP tools{' '}
                      <span className="text-muted-foreground text-xs">(max {config.autoApprove.maxMcpToolsCount}, optional)</span>
                    </Label>
                    <Select
                      value={form.automated_mcp_tools[0] || ''}
                      onValueChange={(v) => {
                        if (v && !form.automated_mcp_tools.includes(v)) {
                          update('automated_mcp_tools', [...form.automated_mcp_tools, v]);
                        }
                      }}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Add MCP tools…" />
                      </SelectTrigger>
                      <SelectContent>
                        {mcpTools.length === 0 ? (
                          <div className="px-3 py-2 text-sm text-muted-foreground">
                            No MCP tools available
                          </div>
                        ) : (
                          mcpTools
                            .filter(tool => !form.automated_mcp_tools.includes(tool.name))
                            .map((tool) => (
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
                    {form.automated_mcp_tools.length > 0 && (
                      <div className="flex flex-wrap gap-2">
                        {form.automated_mcp_tools.map((tool) => (
                          <Badge key={tool} variant="secondary" className="gap-1">
                            {tool}
                            <button
                              type="button"
                              onClick={() => {
                                update('automated_mcp_tools', form.automated_mcp_tools.filter(t => t !== tool));
                              }}
                              className="ml-1 hover:text-destructive"
                            >
                              ×
                            </button>
                          </Badge>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Extended thinking (only for Claude/Gemini models) */}
                  {(form.automated_model.startsWith('claude') || form.automated_model.startsWith('gemini')) && (
                    <>
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          id="automated_enable_thinking"
                          checked={form.automated_enable_thinking}
                          onChange={(e) => update('automated_enable_thinking', e.target.checked)}
                          className="h-4 w-4"
                        />
                        <Label htmlFor="automated_enable_thinking" className="cursor-pointer">
                          Enable extended thinking
                        </Label>
                      </div>

                      {form.automated_enable_thinking && (
                        <div className="grid gap-1.5">
                          <Label>Thinking level</Label>
                          <Select
                            value={form.automated_thinking_level}
                            onValueChange={(v) => update('automated_thinking_level', v)}
                          >
                            <SelectTrigger>
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="minimal">Minimal</SelectItem>
                              <SelectItem value="low">Low</SelectItem>
                              <SelectItem value="medium">Medium</SelectItem>
                              <SelectItem value="high">High</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                      )}
                    </>
                  )}
                </>
              )}
            </>
          )}

          {/* Voice call toggle — dispatch job as a phone call via voice-agent */}
          {form.job_type === 'task' && (
            <div className="flex items-center justify-between rounded-lg border p-3">
              <div className="space-y-0.5">
                <Label htmlFor="voice_call" className="cursor-pointer">Voice call</Label>
                <p className="text-xs text-muted-foreground">
                  Dispatch this job as a phone call via the voice agent.
                </p>
              </div>
              <Switch
                id="voice_call"
                checked={form.voice_call}
                onCheckedChange={(checked) => update('voice_call', checked)}
              />
            </div>
          )}

          {/* Prompt for task jobs */}
          {form.job_type === 'task' && (
            <div className="grid gap-1.5">
              <Label htmlFor="prompt">
                Prompt{' '}
                <span className="text-muted-foreground text-xs">(optional)</span>
              </Label>
              <Textarea
                id="prompt"
                rows={3}
                value={form.prompt}
                onChange={(e) => update('prompt', e.target.value)}
                placeholder="Specific task or instruction for this execution (leave empty for default behavior)…"
              />
              <p className="text-xs text-muted-foreground">
                {form.sub_agent_mode === 'automated' 
                  ? 'Optional task-specific instruction. If empty, the agent will follow its configured system prompt.'
                  : 'This instruction will be sent to the sub-agent. If empty, defaults to "Execute your configured task."'}
              </p>
            </div>
          )}

          {/* Watch fields */}
          {form.job_type === 'watch' && (
            <>
              {/* AI generate – shown always for watch jobs */}
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
                  value={form.check_tool}
                  onValueChange={(v) => update('check_tool', v)}
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
                <Label htmlFor="check_args">
                  Check arguments{' '}
                  <span className="text-muted-foreground text-xs">(JSON, optional)</span>
                </Label>
                <Textarea
                  id="check_args"
                  rows={2}
                  value={form.check_args_text}
                  onChange={(e) => update('check_args_text', e.target.value)}
                  placeholder='{"key": "value"}'
                  className="font-mono text-xs"
                />
              </div>

              {/* Condition expression */}
              <div className="grid gap-1.5">
                <Label htmlFor="condition_expr">
                  Condition expression{' '}
                  <span className="text-muted-foreground text-xs">(JSONPath)</span>
                </Label>
                <Input
                  id="condition_expr"
                  value={form.condition_expr}
                  onChange={(e) => update('condition_expr', e.target.value)}
                  placeholder="$.status"
                />
              </div>

              {/* Expected value */}
              <div className="grid gap-1.5">
                <Label htmlFor="expected_value">
                  Expected value{' '}
                  <span className="text-muted-foreground text-xs">(leave empty to check "is not null")</span>
                </Label>
                <Input
                  id="expected_value"
                  value={form.expected_value}
                  onChange={(e) => update('expected_value', e.target.value)}
                  placeholder="success"
                />
              </div>

              {/* LLM Condition */}
              <div className="grid gap-1.5">
                <Label htmlFor="llm_condition">
                  LLM condition{' '}
                  <span className="text-muted-foreground text-xs">(optional - uses GPT-4o-mini)</span>
                </Label>
                <Textarea
                  id="llm_condition"
                  rows={2}
                  value={form.llm_condition}
                  onChange={(e) => update('llm_condition', e.target.value)}
                  placeholder="The status indicates success or completion"
                />
                <p className="text-xs text-muted-foreground">
                  Natural language condition evaluated by LLM. Use when exact matching is not suitable. Takes precedence over expected value when provided.
                </p>
              </div>

              {/* Destroy after trigger */}
              <div className="flex items-center space-x-2">
                <Checkbox
                  id="destroy_after_trigger"
                  checked={form.destroy_after_trigger}
                  onCheckedChange={(checked) => update('destroy_after_trigger', checked === true)}
                />
                <Label
                  htmlFor="destroy_after_trigger"
                  className="text-sm font-normal cursor-pointer"
                >
                  Disable job after first successful trigger
                </Label>
              </div>
              <p className="text-xs text-muted-foreground -mt-1 ml-6">
                When enabled (default), the watch will automatically be disabled after the condition is met once. Disable this to keep the watch running indefinitely.
              </p>
            </>
          )}

          {/* Notification message for watch jobs */}
          {form.job_type === 'watch' && (
            <div className="grid gap-1.5">
              <Label htmlFor="notification_message">Notification message (optional)</Label>
              <Textarea
                id="notification_message"
                rows={3}
                value={form.notification_message}
                onChange={(e) => update('notification_message', e.target.value)}
                placeholder="Leave empty to auto-generate with LLM."
              />
              <p className="text-xs text-muted-foreground">
                If left empty, an LLM will generate a notification message based on the check result.
              </p>
            </div>
          )}

          {/* Delivery channel */}
          <div className="grid gap-1.5">
            <Label>Delivery channel <span className="text-muted-foreground text-xs">(optional)</span></Label>
            <Select
              value={form.delivery_channel || '_none'}
              onValueChange={(v) => {
                update('delivery_channel', v === '_none' ? '' : v);
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
        <div className="text-muted-foreground text-sm">Loading…</div>
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
                        <Button
                          variant="ghost"
                          size="sm"
                          title="Pause"
                          disabled={pauseMutation.isPending}
                          onClick={() => pauseMutation.mutate(job.id)}
                        >
                          <Pause className="h-4 w-4" />
                        </Button>
                      ) : (
                        <Button
                          variant="ghost"
                          size="sm"
                          title="Resume"
                          disabled={resumeMutation.isPending}
                          onClick={() => resumeMutation.mutate(job.id)}
                        >
                          <Play className="h-4 w-4" />
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        title="Delete"
                        className="text-destructive hover:text-destructive"
                        disabled={deleteMutation.isPending}
                        onClick={() => setDeleteTarget(job)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
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
