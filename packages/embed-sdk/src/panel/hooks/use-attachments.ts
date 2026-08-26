/**
 * Attachment upload state, OUTSIDE the chat state machine: files upload via
 * REST first (adapter `uploadFiles`), and only the resulting wire info rides
 * the eventual send (`metadata.attachments`). Upload state never enters
 * `useChat` messages.
 */
import { useCallback, useRef, useState } from 'react';
import type { UploadedFileInfo } from '../../react';
import { useChatEngine } from '../engine';

export interface AttachmentItem {
  /** Local identity for rendering/removal. */
  key: string;
  name: string;
  mimeType: string;
  status: 'uploading' | 'done' | 'error';
  info?: UploadedFileInfo;
}

export interface UseAttachmentsValue {
  items: AttachmentItem[];
  /** Upload files for the given conversation; resolved infos land in `items`. */
  add: (conversationId: string, files: File[]) => Promise<void>;
  remove: (key: string) => void;
  clear: () => void;
  /** The wire payload for a send — only successfully uploaded files. */
  readyFiles: () => Array<{ uri: string; mimeType: string; name: string; s3Url?: string }>;
}

export function useAttachments(): UseAttachmentsValue {
  const { adapter } = useChatEngine();
  const [items, setItems] = useState<AttachmentItem[]>([]);
  const counterRef = useRef(0);

  const add = useCallback(
    async (conversationId: string, files: File[]) => {
      if (files.length === 0) return;
      const staged: AttachmentItem[] = files.map((file) => ({
        key: `att-${++counterRef.current}`,
        name: file.name,
        mimeType: file.type || 'application/octet-stream',
        status: 'uploading',
      }));
      setItems((prev) => [...prev, ...staged]);
      try {
        const uploaded = await adapter.api.uploadFiles(
          conversationId,
          files.map((file) => ({ file, name: file.name })),
        );
        setItems((prev) =>
          prev.map((item) => {
            const stagedIndex = staged.findIndex((s) => s.key === item.key);
            if (stagedIndex === -1) return item;
            const info = uploaded[stagedIndex];
            return info ? { ...item, status: 'done', info } : { ...item, status: 'error' };
          }),
        );
      } catch {
        const keys = new Set(staged.map((s) => s.key));
        setItems((prev) =>
          prev.map((item) => (keys.has(item.key) ? { ...item, status: 'error' } : item)),
        );
      }
    },
    [adapter],
  );

  const remove = useCallback((key: string) => {
    setItems((prev) => prev.filter((item) => item.key !== key));
  }, []);

  const clear = useCallback(() => setItems([]), []);

  const readyFiles = useCallback(
    () =>
      items
        .filter((item): item is AttachmentItem & { info: UploadedFileInfo } => item.status === 'done' && !!item.info)
        .map((item) => ({
          uri: item.info.uri,
          mimeType: item.info.mimeType,
          name: item.info.name,
          s3Url: item.info.s3Url,
        })),
    [items],
  );

  return { items, add, remove, clear, readyFiles };
}
