/**
 * Scheduler API shim
 *
 * Re-exports generated types so callers have a single import path, and adds the small
 * amount of behaviour the generated operations do not carry: error messages worth
 * showing a person (`formatApiError`), and the 428 risk handshake on a tool invoke.
 *
 * The five AI/validation/invoke calls go through the generated SDK. They used to be
 * hand-rolled `(client as any).post({url})` with hand-written result types, because the
 * backend read those bodies with raw `request.json()` and the SDK could not describe
 * them; that is no longer true of any of them, and
 * `packages/console-frontend/AGENTS.md` asks for the generated client. The remaining
 * raw calls below are the CRUD ones whose bodies are still wider than the generated
 * types (createScheduledJob's delivery_channel_id, for one).
 */
import { client } from './generated/client.gen';
import {
  generateConditionApiV1SchedulerGenerateConditionPost,
  listChannelsApiV1DeliveryChannelsGet,
  listRunsApiV1SchedulerJobsJobIdRunsGet,
  schedulerListJobs,
  schedulerListSharedJobs,
  generateJobDraftApiV1SchedulerGenerateJobDraftPost,
  invokeMcpToolApiV1McpToolsInvokePost,
  resumeParkedRunApiV1SchedulerJobsJobIdRunsRunIdResumePost,
  validateArgsExprApiV1SchedulerValidateArgsExprPost,
  validateConditionApiV1SchedulerValidateConditionPost,
} from './generated/sdk.gen';
import type {
  GenerateConditionRequest,
  JobRunStatus,
  GenerateConditionResponse,
  McpToolInvokeResponse,
  McpToolRisk,
  RunNowResponse,
  ScheduledJob,
  ScheduledJobDraft,
  ScheduledJobRun,
  SharedJobDefinition,
  ValidateArgsExprRequest,
  ValidateArgsExprResponse,
  ValidateConditionRequest,
  ValidateConditionResponse,
} from './generated/types.gen';

export type { RunNowResponse };

// Re-export generated types so pages import from one place.
export type {
  JobRunStatus,
  JobType,
  ScheduleKind,
  ScheduledJob,
  ScheduledJobCreate,
  ScheduledJobRun,
  ScheduledJobUpdate,
} from './generated/types.gen';

/**
 * Turn an API error into something worth showing a person.
 *
 * FastAPI answers a validation failure with `detail` as a list of `{loc, msg}` objects,
 * which `String(...)` renders as "[object Object]". That started mattering when the API
 * began rejecting unparsable watch conditions: the message explains which syntax works,
 * and it was being thrown away.
 */
export function formatApiError(error: unknown): string {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const lines = detail
      .map((item) => {
        if (typeof item === 'string') return item;
        const { loc, msg } = (item ?? {}) as { loc?: unknown[]; msg?: string };
        if (!msg) return null;
        // Skip the "body" prefix every FastAPI location carries.
        const field = Array.isArray(loc) ? loc.filter((p) => p !== 'body').join('.') : '';
        return field ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (lines.length) return lines.join('\n');
  }
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: string }).message;
    if (message) return message;
  }
  if (error instanceof Error) return error.message;
  try {
    return JSON.stringify(detail ?? error);
  } catch {
    return String(error);
  }
}

// ---------------------------------------------------------------------------
// Delivery channels
// ---------------------------------------------------------------------------

export interface DeliveryChannel {
  id: number;
  name: string;
  description?: string | null;
  webhook_url: string;
  client_id: string;
  registered_by: string;
  installation_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DeliveryChannelPage {
  channels: DeliveryChannel[];
  total: number;
}

/**
 * Delivery channels, optionally one page at a time.
 *
 * Console users receive all channels; machine clients receive only their own.
 * Passing no options keeps the old behaviour — every channel — which the job
 * editor's channel picker relies on.
 */
export async function getDeliveryChannels(opts?: {
  search?: string;
  page?: number;
  limit?: number;
}): Promise<DeliveryChannelPage> {
  const { data, error } = await listChannelsApiV1DeliveryChannelsGet({
    query: {
      search: opts?.search || undefined,
      ...(opts?.limit !== undefined ? { limit: opts.limit, page: opts.page ?? 1 } : {}),
    },
  });
  if (error) throw error;
  const body = data!;
  return { channels: body.channels as DeliveryChannel[], total: body.total ?? body.channels.length };
}

export interface DeliveryChannelUpdate {
  name?: string;
  description?: string | null;
  webhook_url?: string;
  secret?: string;
}

/** Partially update a delivery channel. */
export async function updateDeliveryChannel(
  id: number,
  patch: DeliveryChannelUpdate,
): Promise<DeliveryChannel> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).patch({
    url: `/api/v1/delivery-channels/${id}`,
    body: patch,
  });
  if (error) throw error;
  return data as DeliveryChannel;
}

/** Delete a delivery channel. */
export async function deleteDeliveryChannel(id: number): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { error } = await (client as any).delete({
    url: `/api/v1/delivery-channels/${id}`,
  });
  if (error) throw error;
}

// ---------------------------------------------------------------------------
// AI-assisted watch parameter generation
// ---------------------------------------------------------------------------

export type { ConditionEvaluation } from './generated/types.gen';

/**
 * A partial scheduled job. The backend derives it from its create model, so these are
 * the create-body field names and a draft applies field for field.
 */
export type { ScheduledJobDraft } from './generated/types.gen';

/**
 * Draft a whole scheduled job from a one-line description: job type, schedule, check
 * tool and arguments, condition, outcome and delivery. Fields the generator cannot infer
 * are omitted for the form to fill in. The tools the draft may pick from are the user's
 * own catalogue, ranked against the query on the backend — nothing is posted from here.
 * A generation that infers nothing at all is an error, not an empty draft.
 *
 * With `current`, the query is a change to that job ("also tell me when it is
 * cancelled") and the answer is the whole job with the change applied, so the caller
 * can diff it against what it sent. `result` is a real response of the check tool, for
 * writing and verifying the expression against.
 */
export async function generateJobDraft(
  query: string,
  edit?: { current: ScheduledJobDraft; result?: unknown },
): Promise<ScheduledJobDraft> {
  const { data, error } = await generateJobDraftApiV1SchedulerGenerateJobDraftPost({
    body: { query, ...(edit && { current: edit.current, result: edit.result ?? null }) },
  });
  if (error) throw error;
  return data as ScheduledJobDraft;
}

export type { GenerateConditionResponse } from './generated/types.gen';

/**
 * Write or refine just the condition of a watch. Narrower than generateJobDraft: it
 * sees the real response shape, so it writes field paths that exist — and the backend
 * compiles, evaluates and repairs every candidate before returning it.
 */
export async function generateCondition(
  body: GenerateConditionRequest,
): Promise<GenerateConditionResponse> {
  const { data, error } = await generateConditionApiV1SchedulerGenerateConditionPost({ body });
  if (error) throw new Error(formatApiError(error));
  return data as GenerateConditionResponse;
}

export type { ValidateArgsExprResponse } from './generated/types.gen';

/**
 * Resolve a dynamic-arguments expression against the current time — the same
 * resolution the scheduler performs before each check-tool call.
 */
export async function validateArgsExpr(
  body: ValidateArgsExprRequest,
): Promise<ValidateArgsExprResponse> {
  const { data, error } = await validateArgsExprApiV1SchedulerValidateArgsExprPost({ body });
  if (error) throw new Error(formatApiError(error));
  return data as ValidateArgsExprResponse;
}

// ---------------------------------------------------------------------------
// One-off MCP tool call ("run the check now")
// ---------------------------------------------------------------------------

export type { McpToolInvokeResponse, McpToolRisk } from './generated/types.gen';

/** Thrown when the backend wants the caller to confirm a tool that may change data. */
export class McpToolRiskError extends Error {
  readonly risk?: McpToolRisk;
  constructor(message: string, risk?: McpToolRisk) {
    super(message);
    this.name = 'McpToolRiskError';
    this.risk = risk;
  }
}

/**
 * Run one MCP tool with the current user's credentials and return its response, so a
 * watch condition can be written against a real payload.
 *
 * The call is real: a tool that writes will write. The backend answers 428 for any tool
 * it cannot confirm is read-only; re-call with `acknowledgeRisk` once the user agrees.
 */
export async function invokeMcpTool(
  toolName: string,
  args: Record<string, unknown>,
  opts: { serverSlug?: string | null; acknowledgeRisk?: boolean } = {},
): Promise<McpToolInvokeResponse> {
  const { data, error, response } = await invokeMcpToolApiV1McpToolsInvokePost({
    body: {
      tool_name: toolName,
      arguments: args,
      server_slug: opts.serverSlug ?? null,
      acknowledge_risk: opts.acknowledgeRisk ?? false,
    },
  });
  if (error) {
    const detail = (error as { detail?: unknown }).detail;
    if (response?.status === 428 && detail && typeof detail === 'object') {
      const d = detail as { message?: string; risk?: McpToolRisk };
      throw new McpToolRiskError(d.message ?? 'This tool may change data.', d.risk);
    }
    throw new Error(formatApiError(error));
  }
  return data as McpToolInvokeResponse;
}

// ---------------------------------------------------------------------------
// Condition validation ("what would this condition do?")
// ---------------------------------------------------------------------------

export type { ValidateConditionResponse } from './generated/types.gen';

/**
 * Try a condition against a payload without creating a job.
 *
 * The expression language is narrower than people expect — `&&`, `!` and method
 * calls are parse errors — so an untested condition can look right and silently
 * never fire.
 */
export async function validateCondition(
  body: ValidateConditionRequest,
): Promise<ValidateConditionResponse> {
  const { data, error } = await validateConditionApiV1SchedulerValidateConditionPost({ body });
  if (error) throw new Error(formatApiError(error));
  return data as ValidateConditionResponse;
}

// ---------------------------------------------------------------------------
// Create job (extended body that supports delivery_channel)
// ---------------------------------------------------------------------------

/**
 * Automated sub-agent configuration for inline creation in scheduled jobs.
 */
export interface AutomatedSubAgentConfig {
  name: string;
  description: string;
  // Exactly one of model / model_tier (backend validates the XOR). A tier follows the fleet
  // default for that tier, so it survives a model retirement — unlike a pinned alias.
  model?: string | null;
  model_tier?: 'low' | 'standard' | 'premium' | null;
  system_prompt: string;
  mcp_tools?: string[] | null;
  enable_thinking?: boolean | null;
  thinking_level?: string | null;
}

/**
 * Extended create-job payload that references a registered delivery channel by ID.
 */
export interface ScheduledJobCreateExtended {
  name: string;
  job_type: string;
  schedule_kind: string;
  cron_expr?: string;
  interval_seconds?: number;
  run_at?: string;
  sub_agent_id?: number;
  sub_agent_parameters?: AutomatedSubAgentConfig;
  prompt?: string;
  notification_message?: string;
  check_tool?: string;
  check_args?: Record<string, unknown>;
  /** Dynamic arguments: argument name → CEL expression, resolved and merged each run. */
  check_args_exprs?: Record<string, string>;
  /** CEL condition over `result`, `now` and `prev`. A watch needs this, llm_condition, or both. */
  cel_expr?: string;
  llm_condition?: string;
  destroy_after_trigger?: boolean;
  /** Registered delivery channel ID. */
  delivery_channel_id?: number;
  max_failures?: number;
  /** When true, the agent response is delivered as a voice call. */
  voice_call?: boolean;
}

export async function createScheduledJob(body: ScheduledJobCreateExtended): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: '/api/v1/scheduler/jobs',
    body,
  });
  if (error) throw new Error(formatApiError(error));
  return data as ScheduledJob;
}

// ---------------------------------------------------------------------------
// Update job (extended body that supports delivery_channel + sub_agent_id)
// ---------------------------------------------------------------------------

export interface ScheduledJobUpdateExtended {
  name?: string | null;
  schedule_kind?: string | null;
  cron_expr?: string | null;
  interval_seconds?: number | null;
  run_at?: string | null;
  message?: string | null;
  sub_agent_id?: number | null;
  check_tool?: string | null;
  check_args?: Record<string, unknown> | null;
  /** Dynamic arguments; null clears them. */
  check_args_exprs?: Record<string, string> | null;
  /** CEL condition; null clears it, leaving llm_condition as the whole condition. */
  cel_expr?: string | null;
  llm_condition?: string | null;
  delivery_channel_id?: number | null;
  max_failures?: number | null;
  enabled?: boolean | null;
}

export async function updateScheduledJob(
  jobId: number,
  body: ScheduledJobUpdateExtended,
): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).patch({
    url: `/api/v1/scheduler/jobs/${jobId}`,
    body,
  });
  if (error) throw new Error(formatApiError(error));
  return data as ScheduledJob;
}

// ---------------------------------------------------------------------------
// Run-now (full end-to-end test run including webhook delivery)
// ---------------------------------------------------------------------------

/**
 * Immediately dispatch a saved job through the full execution pipeline:
 * offline-token resolution → agent-runner (A2A) → webhook delivery → run record.
 *
 * Returns 202 immediately with the pre-created run_id. The result is delivered
 * via the scheduler_notification WebSocket event when execution completes.
 */
export async function runJobNow(jobId: number): Promise<RunNowResponse> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: `/api/v1/scheduler/jobs/${jobId}/run-now`,
    body: {},
  });
  if (error) throw new Error(formatApiError(error));
  return data as RunNowResponse;
}

/**
 * Answer a run parked on the owner's authorization.
 *
 * The console is the second surface for this ask — the first is the job's delivery
 * channel, where most owners live. It matters that it is offered here too: a person
 * looking at a stopped job should be able to restart it from the page that told them
 * it was stopped, rather than be sent to find a chat message.
 *
 * Goes through the generated operation rather than a raw post: the body is a single
 * literal union the backend already describes, so there is nothing wider to carry.
 */
export async function resumeParkedRun(
  jobId: number,
  runId: number,
  decision: 'approved' | 'declined',
): Promise<RunNowResponse> {
  const { data, error } = await resumeParkedRunApiV1SchedulerJobsJobIdRunsRunIdResumePost({
    path: { job_id: jobId, run_id: runId },
    body: { decision },
  });
  if (error) throw new Error(formatApiError(error));
  return data as RunNowResponse;
}

// ---------------------------------------------------------------------------
// Additional CRUD operations for scheduler pages
// ---------------------------------------------------------------------------

export interface ScheduledJobPage {
  jobs: ScheduledJob[];
  total: number;
}

/**
 * Scheduled jobs, optionally one page at a time.
 *
 * The endpoint is an MCP tool, so its body stays a bare array for the agents
 * that call it; the count the console needs for pagination comes back in the
 * `X-Total-Count` header, read off the generated operation's `response`.
 * Passing no options returns every job.
 */
export async function listJobs(opts?: {
  search?: string;
  page?: number;
  limit?: number;
}): Promise<ScheduledJobPage> {
  const { data, error, response } = await schedulerListJobs({
    query: {
      search: opts?.search || undefined,
      ...(opts?.limit !== undefined ? { limit: opts.limit, page: opts.page ?? 1 } : {}),
    },
  });
  if (error) throw new Error(formatApiError(error));
  const jobs = data ?? [];
  // Absent when the caller asked for everything, and on any proxy that drops it;
  // the page length is then the honest count.
  const header = response?.headers?.get?.('X-Total-Count');
  const total = header != null && header !== '' ? Number(header) : jobs.length;
  return { jobs, total: Number.isFinite(total) ? total : jobs.length };
}

export interface SharedJobDefinitionPage {
  definitions: SharedJobDefinition[];
  total: number;
}

/**
 * Shared job definitions, optionally one page at a time.
 *
 * Mirrors `listJobs` — same MCP-tool constraint, so the body is a bare array and
 * the count travels in `X-Total-Count`. It is the tanstack *query wrapper* that
 * discards the response object; the generated operation itself exposes it, so
 * this is a typed call.
 */
export async function listSharedDefinitions(opts?: {
  search?: string;
  subscribed?: boolean;
  page?: number;
  limit?: number;
}): Promise<SharedJobDefinitionPage> {
  const { data, error, response } = await schedulerListSharedJobs({
    query: {
      search: opts?.search || undefined,
      subscribed: opts?.subscribed,
      ...(opts?.limit !== undefined ? { limit: opts.limit, page: opts.page ?? 1 } : {}),
    },
  });
  if (error) throw new Error(formatApiError(error));
  const definitions = data ?? [];
  const header = response?.headers?.get?.('X-Total-Count');
  const total = header != null && header !== '' ? Number(header) : definitions.length;
  return { definitions, total: Number.isFinite(total) ? total : definitions.length };
}

export async function getJob(jobId: number): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({
    url: `/api/v1/scheduler/jobs/${jobId}`,
  });
  if (error) throw new Error(formatApiError(error));
  return data as ScheduledJob;
}

export async function pauseJob(jobId: number): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: `/api/v1/scheduler/jobs/${jobId}/pause`,
    body: {},
  });
  if (error) throw new Error(formatApiError(error));
  return data as ScheduledJob;
}

export async function resumeJob(jobId: number): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: `/api/v1/scheduler/jobs/${jobId}/resume`,
    body: {},
  });
  if (error) throw new Error(formatApiError(error));
  return data as ScheduledJob;
}

export async function deleteJob(jobId: number): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { error } = await (client as any).delete({
    url: `/api/v1/scheduler/jobs/${jobId}`,
  });
  if (error) throw new Error(formatApiError(error));
}

export interface ScheduledJobRunPage {
  runs: ScheduledJobRun[];
  total: number;
}

/**
 * A page of a job's execution history, newest first.
 *
 * Always paged: run history only grows, and the endpoint used to answer with a
 * bare `LIMIT 50` and no offset, so a job's 51st run could not be reached at
 * all. Like the sibling scheduler lists the body is a bare array and the count
 * comes back in `X-Total-Count`.
 */
export async function listRuns(
  jobId: number,
  opts?: { page?: number; limit?: number; status?: JobRunStatus },
): Promise<ScheduledJobRunPage> {
  const { data, error, response } = await listRunsApiV1SchedulerJobsJobIdRunsGet({
    path: { job_id: jobId },
    query: {
      page: opts?.page ?? 1,
      ...(opts?.limit !== undefined ? { limit: opts.limit } : {}),
      ...(opts?.status ? { status: opts.status } : {}),
    },
  });
  if (error) throw new Error(formatApiError(error));
  const runs = data ?? [];
  const header = response?.headers?.get?.('X-Total-Count');
  const total = header != null && header !== '' ? Number(header) : runs.length;
  return { runs, total: Number.isFinite(total) ? total : runs.length };
}
