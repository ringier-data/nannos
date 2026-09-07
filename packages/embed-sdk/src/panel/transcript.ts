/**
 * Plain-text views of a conversation — what "copy" puts on the clipboard and
 * what "export" writes to a file. Only what the user could READ is included:
 * the text parts of each message. Activity lines, thoughts, work plans and tool
 * parts are the turn's machinery, not its content; files are named, not
 * embedded.
 */
import { fileName, type NannosUIMessage } from '../transport';

type Part = NannosUIMessage['parts'][number];

/** The readable text of one message: its text parts, in order. */
export function messagePlainText(message: NannosUIMessage): string {
  return message.parts
    .filter((part): part is Extract<Part, { type: 'text' }> => part.type === 'text')
    .map((part) => part.text)
    .join('\n')
    .trim();
}

/** Attachment names to list under a message (history `file` parts, or the
 *  wire metadata a live send carries). */
function messageFileNames(message: NannosUIMessage): string[] {
  const fromParts = message.parts
    .filter((part): part is Extract<Part, { type: 'file' }> => part.type === 'file')
    .map((part) => fileName(part));
  if (fromParts.length > 0) return fromParts;
  return (message.metadata?.attachments ?? []).map((att) => att.name);
}

export interface TranscriptOptions {
  /** Set when `messages` is not the whole history (older pages not loaded):
   *  the export then says so instead of silently looking complete. */
  truncated?: boolean;
  /** Speaker labels, in the viewer's language. */
  labels: { user: string; assistant: string; truncated: string };
}

/**
 * The conversation as a plain-text transcript: a title, then one block per
 * message with its speaker. Messages with nothing readable (a HITL resume row,
 * a turn that produced only tool calls) leave no block.
 */
export function formatTranscript(
  title: string,
  messages: NannosUIMessage[],
  { truncated = false, labels }: TranscriptOptions,
): string {
  const lines = [title, '='.repeat(title.length), ''];
  if (truncated) lines.push(`⚠ ${labels.truncated}`, '');
  for (const message of messages) {
    if (message.role !== 'user' && message.role !== 'assistant') continue;
    const text = messagePlainText(message);
    const files = messageFileNames(message);
    if (!text && files.length === 0) continue;
    lines.push(`${message.role === 'user' ? labels.user : labels.assistant}:`);
    if (text) lines.push(text);
    for (const name of files) lines.push(`[${name}]`);
    lines.push('');
  }
  return lines.join('\n');
}

/** A filesystem-safe name from a conversation title. */
export function slugifyFilename(title: string): string {
  const slug = title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug || 'conversation';
}

/** Hand the browser a text file to save. The anchor goes on the document body
 *  (never the shadow root) — a click on a detached anchor downloads nothing. */
export function downloadTextFile(filename: string, content: string, mimeType = 'text/plain'): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
