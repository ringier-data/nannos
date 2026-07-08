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
 * Get normalized task state from status object or string.
 * Accepts both A2A v1.0 (`TASK_STATE_*`) and legacy v0.3 (`completed`) forms.
 */
export function getTaskState(status: string | { state?: string; message?: string } | undefined | null): string {
  let raw: string | undefined;
  if (!status) return 'unknown';
  if (typeof status === 'string') raw = status;
  else if (typeof status === 'object') {
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
  // v0.3: { file: { uri, mimeType, name } }
  if (p.file && typeof p.file === 'object' && typeof p.file.uri === 'string') {
    return { uri: p.file.uri, mimeType: p.file.mimeType, name: p.file.name };
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
