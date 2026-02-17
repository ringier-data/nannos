/**
 * Hook for managing file attachments in messages
 * 
 * NOTE: Presigned URL regeneration is handled server-side.
 * The backend generates fresh URLs whenever messages are loaded.
 */

import { extractFileAttachments } from '@/lib/file-utils';
import type { Message } from '../types';

interface UseFileUrlsReturn {
  /** File attachments from the message */
  files: Array<{ uri: string; mimeType?: string; name?: string }>;
}

/**
 * Hook to extract and manage file attachments from a message.
 * 
 * Presigned URLs are hydrated by the backend on message load,
 * so no client-side regeneration is needed.
 * 
 * @param message - The message containing file attachments
 * @returns Object with file attachments
 * 
 * @example
 * ```tsx
 * const { files } = useFileUrls(message);
 * // Render files with pre-hydrated URLs
 * ```
 */
export function useFileUrls(message: Message): UseFileUrlsReturn {
  const files = extractFileAttachments(message.parts);
  return { files };
}

/**
 * Hook to extract and manage file attachments from multiple messages.
 * 
 * @param messages - Array of messages
 * @returns Object with extracted file attachments
 */
export function useBatchFileUrls(messages: Message[]): {
  files: Array<{ uri: string; mimeType?: string; name?: string }>;
} {
  const allFiles = messages.flatMap((msg) => extractFileAttachments(msg.parts));
  return { files: allFiles };
}
