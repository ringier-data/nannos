// Pure A2A protocol helpers — framework-free semantics shared by the Embed SDK
// runtime and console-frontend. No DOM, no styling. Presentational helpers
// (markdown, clipboard, date formatting) live with the UI kit instead.

import { v7 as uuidv7 } from 'uuid';

/** Generate a UUID v7 (used for message/conversation ids). */
export function generateUUID(): string {
  return uuidv7();
}

/** Check if a task status indicates completion. */
export function isTaskComplete(status: string | undefined | null): boolean {
  const normalized = (status || '').toLowerCase();
  return ['completed', 'failed', 'succeeded', 'cancelled'].includes(normalized);
}

/** Check if a task should show progress. */
export function shouldShowTaskProgress(status: string | undefined | null): boolean {
  const normalized = (status || '').toLowerCase();
  return normalized === 'running' || normalized === 'in_progress';
}

/**
 * A2A v1.0 protobuf TaskState enum names -> the short wire strings this app uses.
 * (v0.3 short names like "completed" / "input-required" pass through unchanged.)
 */
const V1_TASK_STATE: Record<string, string> = {
  task_state_submitted: 'submitted',
  task_state_working: 'working',
  task_state_completed: 'completed',
  task_state_failed: 'failed',
  task_state_canceled: 'canceled',
  task_state_input_required: 'input-required',
  task_state_rejected: 'rejected',
  task_state_auth_required: 'auth-required',
  task_state_unspecified: 'unknown',
};

/**
 * The same enum by its protobuf INT value — what console-backend's REST rows
 * carry (`Message.state: int`, message.py). The socket path sends the
 * `TASK_STATE_*` string; history restore gets the number.
 */
const V1_TASK_STATE_BY_INT: Record<number, string> = {
  0: 'unknown',
  1: 'submitted',
  2: 'working',
  3: 'completed',
  4: 'failed',
  5: 'canceled',
  6: 'input-required',
  7: 'rejected',
  8: 'auth-required',
};

/**
 * Get normalized task state from status object, string, or protobuf int.
 * Accepts A2A v1.0 names (`TASK_STATE_*`), their int enum values (REST rows),
 * and legacy v0.3 short forms (`completed`).
 */
export function getTaskState(
  status: string | number | { state?: string | number; message?: string } | undefined | null,
): string {
  let raw: string | undefined;
  if (status === undefined || status === null || status === '') return 'unknown';
  if (typeof status === 'number') return V1_TASK_STATE_BY_INT[status] ?? 'unknown';
  if (typeof status === 'string') raw = status;
  else if (typeof status === 'object') {
    if (typeof status.state === 'number') return V1_TASK_STATE_BY_INT[status.state] ?? 'unknown';
    if (typeof status.state === 'string') raw = status.state;
    else if (typeof status.message === 'string') raw = status.message;
  }
  if (!raw) return 'unknown';
  const lower = raw.toLowerCase();
  return V1_TASK_STATE[lower] ?? lower;
}

/**
 * A2A part kind, resilient to both v1.0 (flat: text/data/url/raw fields) and
 * legacy v0.3 (`kind` discriminator) shapes.
 */
export function getPartKind(part: unknown): 'text' | 'data' | 'file' | undefined {
  if (!part || typeof part !== 'object') return undefined;
  const p = part as Record<string, unknown>;
  if (typeof p.kind === 'string') {
    return p.kind as 'text' | 'data' | 'file';
  }
  if (p.text !== undefined) return 'text';
  if (p.data !== undefined) return 'data';
  if (p.url !== undefined || p.raw !== undefined || p.file !== undefined) return 'file';
  return undefined;
}

/**
 * Normalize a file part's info across A2A v1.0 (flat `url`/`mediaType`/`filename`)
 * and legacy v0.3 (`file: { uri, mimeType, name }`) shapes.
 */
export function getFileInfo(part: unknown): { uri: string; mimeType?: string; name?: string } | null {
  if (!part || typeof part !== 'object') return null;
  const p = part as Record<string, any>;
  // v0.3: { file: { uri, mimeType, name } } — persisted rows carry the python
  // SDK's snake_case dump (`mime_type`), so accept both spellings.
  if (p.file && typeof p.file === 'object' && typeof p.file.uri === 'string') {
    return { uri: p.file.uri, mimeType: p.file.mimeType ?? p.file.mime_type, name: p.file.name };
  }
  // v1.0: flat { url, mediaType, filename }
  if (typeof p.url === 'string') {
    return { uri: p.url, mimeType: p.mediaType, name: p.filename };
  }
  return null;
}

/**
 * Extract text from message parts array
 * Handles A2A Part structure: { root: { text: "..." } } or legacy { text: "..." }
 */
export function extractPartTexts(
  parts: Array<{ root?: { text?: string }; text?: string } | string> | undefined | null
): string[] {
  if (!Array.isArray(parts)) return [];
  return parts.map((part) => {
    // Handle A2A Part structure: { root: { text: "..." } }
    if (part && typeof part === 'object' && 'root' in part && part.root && typeof part.root.text === 'string') {
      return part.root.text;
    }
    // Handle legacy structure: { text: "..." }
    if (part && typeof (part as { text?: string }).text === 'string') {
      return (part as { text?: string }).text!;
    }
    // Handle plain string
    if (typeof part === 'string') {
      return part;
    }
    return '';
  });
}

/** Check if message parts should be displayed. */
export function shouldDisplayMessageParts(parts: Array<{ text?: string }> | undefined | null): boolean {
  if (!Array.isArray(parts) || parts.length === 0) {
    return false;
  }
  const texts = extractPartTexts(parts);
  return texts.some((text) => text && text.trim().length > 0);
}
