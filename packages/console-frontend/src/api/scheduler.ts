/**
 * Scheduler API shim
 *
 * Re-exports generated types so callers have a single import path, and
 * provides manual wrappers for endpoints whose bodies the generated SDK
 * cannot yet describe (because the backend previously used raw `request.json()`).
 *
 * TODO: after running `npm run gen-sdk` following the backend fix,
 * `storeSchedulerConsent` can be removed and replaced by the generated
 * `storeConsentApiV1SchedulerConsentPost`.
 */
import { client } from './generated/client.gen';
import type { RunNowResponse, ScheduledJob, ScheduledJobRun } from './generated/types.gen';

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
  group_ids: number[];
  created_at: string;
  updated_at: string;
}

/**
 * Fetch delivery channels the current user can see (scoped by group membership).
 * Machine clients receive only their own channels.
 */
export async function getDeliveryChannels(): Promise<DeliveryChannel[]> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({
    url: '/api/v1/delivery-channels',
  });
  if (error) throw error;
  return (data as { channels: DeliveryChannel[] }).channels;
}

export interface DeliveryChannelUpdate {
  name?: string;
  description?: string | null;
  webhook_url?: string;
  secret?: string;
  group_ids?: number[];
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

export interface GenerateWatchParamsResponse {
  check_tool?: string | null;
  check_args?: Record<string, unknown> | null;
  condition_expr?: string | null;
  expected_value?: string | null;
  llm_condition?: string | null;
  notification_message?: string | null;
}

/**
 * Call the backend LLM endpoint to pick the best tool and suggest check_args,
 * condition_expr and a notification message for a watch job.
 */
export async function generateWatchParams(
  tools: Record<string, unknown>[],
  query: string,
): Promise<GenerateWatchParamsResponse> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: '/api/v1/scheduler/generate-watch-params',
    body: { tools, query },
  });
  if (error) throw error;
  return data as GenerateWatchParamsResponse;
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
  model: string;
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
  condition_expr?: string;
  expected_value?: string;
  llm_condition?: string;
  destroy_after_trigger?: boolean;
  /** Registered delivery channel ID. */
  delivery_channel_id?: number;
  max_failures?: number;
}

export async function createScheduledJob(body: ScheduledJobCreateExtended): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: '/api/v1/scheduler/jobs',
    body,
  });
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
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
  condition_expr?: string | null;
  expected_value?: string | null;
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
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
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
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
  return data as RunNowResponse;
}

// ---------------------------------------------------------------------------
// Additional CRUD operations for scheduler pages
// ---------------------------------------------------------------------------

export async function listJobs(): Promise<ScheduledJob[]> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({
    url: '/api/v1/scheduler/jobs',
  });
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
  return data as ScheduledJob[];
}

export async function getJob(jobId: number): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({
    url: `/api/v1/scheduler/jobs/${jobId}`,
  });
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
  return data as ScheduledJob;
}

export async function pauseJob(jobId: number): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: `/api/v1/scheduler/jobs/${jobId}/pause`,
    body: {},
  });
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
  return data as ScheduledJob;
}

export async function resumeJob(jobId: number): Promise<ScheduledJob> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).post({
    url: `/api/v1/scheduler/jobs/${jobId}/resume`,
    body: {},
  });
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
  return data as ScheduledJob;
}

export async function deleteJob(jobId: number): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { error } = await (client as any).delete({
    url: `/api/v1/scheduler/jobs/${jobId}`,
  });
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
}

export async function listRuns(jobId: number, limit?: number): Promise<ScheduledJobRun[]> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data, error } = await (client as any).get({
    url: `/api/v1/scheduler/jobs/${jobId}/runs`,
    query: limit ? { limit } : undefined,
  });
  if (error) throw new Error(typeof error === 'object' && error !== null && 'detail' in error
    ? String((error as { detail: unknown }).detail)
    : String(error));
  return data as ScheduledJobRun[];
}
