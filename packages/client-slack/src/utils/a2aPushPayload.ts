import { Task } from '@a2a-js/sdk';

/**
 * Classified A2A push-notification event.
 *   - `task`    — a Task we can act on (caller still filters by `status.state`).
 *   - `ignored` — a recognized A2A event we don't act on (artifact/message stream);
 *                 the caller should acknowledge it with 200, not reject it.
 *   - `invalid` — not a recognized A2A push payload.
 */
export type A2APushEvent =
  | { type: 'task'; task: Task }
  | { type: 'ignored'; reason: string }
  | { type: 'invalid' };

/**
 * Parse an inbound A2A push-notification body into a classified event.
 *
 * The A2A Python SDK (>= 1.x, `BasePushNotificationSender`) serializes each event
 * as a protobuf `StreamResponse` via `MessageToDict`. That envelope is a oneof —
 * exactly one of `task` / `statusUpdate` / `artifactUpdate` / `message` is set —
 * and the inner messages use protobuf-JSON conventions that differ from the A2A
 * JSON spec the `@a2a-js/sdk` types model:
 *   - no `kind` discriminator on Task / Message / Part
 *   - `status.state` is the enum NAME, e.g. `"TASK_STATE_COMPLETED"`
 *   - a text part is `{ "text": "..." }` (no `kind: "text"`)
 *
 * Scheduled-run completions arrive as a terminal `statusUpdate`
 * (`TaskStatusUpdateEvent`) whose `status.message` carries the payload — so both
 * `task` and `statusUpdate` are surfaced as actionable Tasks. Streaming
 * `artifactUpdate`/`message` events are surfaced as `ignored`. Already-JSON-spec
 * bodies (e.g. a future server-side v0.3 sender) pass through unchanged.
 */
export function parseA2APushEvent(body: unknown): A2APushEvent {
  if (!isObject(body)) return { type: 'invalid' };

  // protobuf StreamResponse oneof envelope
  if (isObject(body.task)) return taskResult(body.task);
  if (isObject(body.statusUpdate)) return taskFromStatusUpdate(body.statusUpdate);
  if (isObject(body.artifactUpdate)) return { type: 'ignored', reason: 'artifact-update' };
  if (isObject(body.message)) return { type: 'ignored', reason: 'message' };

  // already-JSON-spec (un-enveloped) A2A payloads
  if (body.kind === 'task' && isObject(body.status)) return taskResult(body);
  if (body.kind === 'status-update' && isObject(body.status)) return taskFromStatusUpdate(body);
  if (body.kind === 'artifact-update') return { type: 'ignored', reason: 'artifact-update' };
  if (body.kind === 'message') return { type: 'ignored', reason: 'message' };

  return { type: 'invalid' };
}

function taskResult(rawTask: Record<string, unknown>): A2APushEvent {
  if (!isObject(rawTask.status)) return { type: 'invalid' };
  return {
    type: 'task',
    task: { ...rawTask, kind: 'task', status: normalizeStatus(rawTask.status) } as unknown as Task,
  };
}

/** Synthesize a Task from a TaskStatusUpdateEvent (`{taskId, contextId, status}`). */
function taskFromStatusUpdate(event: Record<string, unknown>): A2APushEvent {
  if (!isObject(event.status)) return { type: 'invalid' };
  return {
    type: 'task',
    task: {
      kind: 'task',
      id: event.taskId ?? event.task_id ?? event.id,
      contextId: event.contextId ?? event.context_id,
      status: normalizeStatus(event.status),
    } as unknown as Task,
  };
}

function normalizeStatus(status: Record<string, unknown>): Record<string, unknown> {
  return {
    ...status,
    state: normalizeTaskState(status.state),
    ...(isObject(status.message) ? { message: normalizeMessage(status.message) } : {}),
  };
}

/** `TASK_STATE_COMPLETED` -> `completed`; passes through already-normalized states. */
function normalizeTaskState(state: unknown): unknown {
  if (typeof state !== 'string' || !state.startsWith('TASK_STATE_')) return state;
  return state.slice('TASK_STATE_'.length).toLowerCase().replace(/_/g, '-');
}

function normalizeMessage(message: Record<string, unknown>): Record<string, unknown> {
  return {
    ...message,
    kind: 'message',
    ...(Array.isArray(message.parts) ? { parts: message.parts.map(normalizePart) } : {}),
  };
}

/** Map a protobuf `Part` oneof to a JSON-spec part; pass through if already tagged. */
function normalizePart(part: unknown): unknown {
  if (!isObject(part)) return part;
  if (typeof part.kind === 'string') return part; // already JSON-spec
  if (typeof part.text === 'string') {
    return { kind: 'text', text: part.text, ...(isObject(part.metadata) ? { metadata: part.metadata } : {}) };
  }
  if (part.data !== undefined) {
    return { kind: 'data', data: part.data, ...(isObject(part.metadata) ? { metadata: part.metadata } : {}) };
  }
  if (part.url !== undefined || part.raw !== undefined || part.filename !== undefined) {
    return {
      kind: 'file',
      file: {
        ...(typeof part.url === 'string' ? { uri: part.url } : {}),
        ...(typeof part.raw === 'string' ? { bytes: part.raw } : {}),
        ...(typeof part.filename === 'string' ? { name: part.filename } : {}),
        ...(typeof part.media_type === 'string' ? { mimeType: part.media_type } : {}),
      },
    };
  }
  return part;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null;
}
