/**
 * File utilities for managing file attachments in messages
 */

interface FileAttachment {
  uri: string;
  mimeType?: string;
  name?: string;
}

/**
 * Extract file attachments from message parts.
 * 
 * @param parts - Message parts array
 * @returns Array of file attachments
 */
export function extractFileAttachments(
  parts?: Array<{ kind: string; file?: FileAttachment }>
): FileAttachment[] {
  if (!parts) return [];
  
  return parts
    .filter((part) => part.kind === 'file' && part.file)
    .map((part) => part.file!);
}
