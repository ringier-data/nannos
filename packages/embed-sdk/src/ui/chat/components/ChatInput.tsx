import { useState, useRef, useEffect, type KeyboardEvent, type ChangeEvent } from 'react';
import { Send, AlertTriangle, Mic, X, Paperclip, Square } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useChat } from '../contexts';
import { useHostAdapter } from '../../adapter';
import { AudioRecorder } from './AudioRecorder';
import { toast } from 'sonner';

interface PendingFile {
  id: string;
  file: File;
  name: string;
  type: string;
  size: number;
  previewUrl?: string;
}

export function ChatInput() {
  const { sendMessage, isConnected, isWaiting, interruptTask, activeConversationId, activeConversationReadOnly } =
    useChat();
  const { isImpersonating, api } = useHostAdapter();
  const [value, setValue] = useState('');
  const [isRecording, setIsRecording] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragCounterRef = useRef(0);

  const canSend = isConnected && (value.trim().length > 0 || pendingFiles.length > 0) && !isUploading;

  const handleSend = async () => {
    if (!canSend) return;

    // For file uploads we need an existing conversation ID; if none exists yet we can't
    // upload in a single step — bail out so the user sees the connected-but-no-conversation state.
    if (pendingFiles.length > 0 && !activeConversationId) return;

    let fileAttachments: Array<{
      uri: string;
      mimeType: string;
      name: string;
      s3Url: string;
    }> = [];

    // If there are pending files, upload them first
    if (pendingFiles.length > 0) {
      setIsUploading(true);
      try {
        const uploaded = await api.uploadFiles(
          activeConversationId!,
          pendingFiles.map((pendingFile) => ({ file: pendingFile.file, name: pendingFile.name })),
        );

        // Store file data with both uri (for display) and s3Url (for storage)
        fileAttachments = uploaded.map((file) => ({
          uri: file.uri, // presigned URL for immediate display
          mimeType: file.mimeType,
          name: file.name,
          s3Url: file.s3Url, // s3:// URL for storage and regeneration
        }));

        // Clear pending files after successful upload
        clearPendingFiles();
      } catch (error) {
        console.error('File upload error:', error);
        toast.error(error instanceof Error ? error.message : 'Failed to upload files');
        setIsUploading(false);
        return;
      } finally {
        setIsUploading(false);
      }
    }

    // Send the message with uploaded files (including s3Urls for backend)
    sendMessage(value.trim(), fileAttachments.length > 0 ? fileAttachments : undefined);
    setValue('');
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleRecordingComplete = (audioBlob: Blob, fileName: string) => {
    // Convert Blob to File
    const audioFile = new File([audioBlob], fileName, { type: audioBlob.type });
    addFile(audioFile);
    setIsRecording(false);
  };

  const handleRecordingCancel = () => {
    setIsRecording(false);
  };

  const addFile = (file: File) => {
    const id = Math.random().toString(36).slice(2);
    const isImage = file.type.startsWith('image/');

    const pendingFile: PendingFile = {
      id,
      file,
      name: file.name,
      type: file.type,
      size: file.size,
      previewUrl: isImage ? URL.createObjectURL(file) : undefined,
    };

    setPendingFiles((prev) => [...prev, pendingFile]);
  };

  const removeFile = (id: string) => {
    setPendingFiles((prev) => {
      const file = prev.find((f) => f.id === id);
      if (file?.previewUrl) {
        URL.revokeObjectURL(file.previewUrl);
      }
      return prev.filter((f) => f.id !== id);
    });
  };

  const clearPendingFiles = () => {
    pendingFiles.forEach((file) => {
      if (file.previewUrl) {
        URL.revokeObjectURL(file.previewUrl);
      }
    });
    setPendingFiles([]);
  };

  const handleFileInputChange = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    files.forEach(addFile);
    // Reset input
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  // Drag and drop handlers
  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
      setIsDragging(true);
    }
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) {
      setIsDragging(false);
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
    dragCounterRef.current = 0;

    if (!isConnected || isUploading) return;

    const files = Array.from(e.dataTransfer.files);
    files.forEach(addFile);
  };

  // Paste handler for images
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;

    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) {
          // Generate a meaningful filename with timestamp
          const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
          const extension = file.type.split('/')[1];
          const renamedFile = new File([file], `pasted-image-${timestamp}.${extension}`, {
            type: file.type,
          });
          addFile(renamedFile);
          toast.success('Image pasted successfully');
        }
      }
    }
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
  };

  // One send action, in every state. While the agent is working the same send is
  // routed by the backend as a steering message for the active task; cancelling
  // is the separate stop button rather than a mode hidden behind a menu.
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
    // Auto-resize textarea
    const textarea = e.target;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  };

  // Focus textarea when connected
  useEffect(() => {
    if (isConnected && textareaRef.current && !isImpersonating) {
      textareaRef.current.focus();
    }
  }, [isConnected, isImpersonating]);

  // Show warning when impersonating
  if (isImpersonating) {
    return (
      <div className="border-t border-border bg-nannos-surface-raised p-4">
        <Alert variant="default" className="border-nannos-warn/50 bg-nannos-warn/10">
          <AlertTriangle className="h-4 w-4 text-nannos-warn" />
          <AlertDescription className="text-nannos-warn">
            Chat is unavailable while impersonating. Chat requires the user's access token which is not available during
            impersonation.
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  // Embedded-widget conversations are read-only outside their host application:
  // their turns act on that page's registered forms/objects, which don't exist here.
  if (activeConversationReadOnly) {
    return (
      <div className="border-t border-border bg-nannos-surface-raised p-4" data-testid="embedded-readonly-notice">
        <Alert variant="default" className="border-nannos-warn/50 bg-nannos-warn/10">
          <AlertTriangle className="h-4 w-4 text-nannos-warn" />
          <AlertDescription className="text-nannos-warn">
            This conversation was created by an embedded assistant in another application and is read-only here.
            Continue it from that application.
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <>
      <div
        className="relative flex flex-col gap-2 border-t border-border bg-nannos-surface-raised p-3"
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        {/* Drag overlay */}
        {isDragging && (
          <div className="absolute inset-0 z-10 flex items-center justify-center rounded-nannos-card bg-nannos-accent-subtle/90 backdrop-blur-sm">
            <div className="text-center">
              <Paperclip className="mx-auto mb-2 h-8 w-8 text-nannos-accent" />
              <p className="text-sm font-medium">Drop files here</p>
            </div>
          </div>
        )}

        {/* Pending files preview */}
        {pendingFiles.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {pendingFiles.map((file) => (
              <div
                key={file.id}
                className="flex items-center gap-2 rounded-nannos-control border border-border bg-nannos-surface px-2 py-1.5 text-sm"
              >
                {file.previewUrl ? (
                  <img src={file.previewUrl} alt={file.name} className="h-8 w-8 rounded object-cover" />
                ) : file.type.startsWith('audio/') ? (
                  <Mic className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <Paperclip className="h-4 w-4 text-muted-foreground" />
                )}
                <div className="flex min-w-0 flex-col">
                  <span className="max-w-[180px] truncate text-xs">{file.name}</span>
                  <span className="text-[11px] text-muted-foreground">{formatFileSize(file.size)}</span>
                </div>
                <Button
                  size="icon"
                  variant="ghost"
                  className="h-6 w-6 shrink-0"
                  onClick={() => removeFile(file.id)}
                  aria-label={`Remove ${file.name}`}
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
          </div>
        )}

        {/* Single composer shell: attachments and mic sit inside the field, so the
            only thing competing with the text is the send action itself. */}
        <div
          className={cn(
            'flex items-end gap-1 rounded-nannos-card border border-border bg-nannos-surface-raised px-1.5 py-1.5',
            'transition-colors focus-within:border-nannos-accent',
            isDragging && 'border-nannos-accent',
          )}
        >
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="image/*,audio/*,application/pdf,application/msword,application/vnd.ms-excel,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.*"
            onChange={handleFileInputChange}
            className="hidden"
          />
          <Button
            size="icon"
            variant="ghost"
            onClick={() => fileInputRef.current?.click()}
            disabled={!isConnected || isUploading}
            className="h-8 w-8 shrink-0 text-muted-foreground hover:text-foreground"
            aria-label="Attach file"
          >
            <Paperclip className="h-4 w-4" />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            onClick={() => setIsRecording(true)}
            disabled={!isConnected || isUploading}
            className="h-8 w-8 shrink-0 text-muted-foreground hover:text-foreground"
            aria-label="Record audio"
          >
            <Mic className="h-4 w-4" />
          </Button>

          <Textarea
            ref={textareaRef}
            value={value}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={
              isUploading
                ? 'Uploading...'
                : !isConnected
                  ? 'Connect to an agent to start chatting...'
                  : isWaiting
                    ? 'Send a follow-up message to steer the agent...'
                    : 'Type your message...'
            }
            disabled={!isConnected || isUploading}
            rows={1}
            className={cn(
              'min-h-8 flex-1 resize-none border-0 bg-transparent px-1 py-1.5 text-sm shadow-none',
              'focus-visible:border-0 focus-visible:ring-0 dark:bg-transparent',
            )}
            data-testid="input-message"
          />

          <Button
            onClick={handleSend}
            disabled={!canSend}
            size="icon"
            className="h-8 w-8 shrink-0 rounded-nannos-control bg-nannos-accent text-nannos-accent-foreground hover:bg-nannos-accent-strong"
            data-testid="button-send"
            aria-label={isWaiting ? 'Send follow-up message' : 'Send message'}
          >
            <Send className="h-4 w-4" />
          </Button>

          {isWaiting && (
            <Button
              onClick={interruptTask}
              size="icon"
              className="h-8 w-8 shrink-0 rounded-nannos-control bg-nannos-danger-soft text-nannos-danger hover:bg-nannos-danger/20"
              data-testid="button-stop"
              aria-label="Stop generation"
            >
              <Square className="h-3.5 w-3.5 fill-current" />
            </Button>
          )}
        </div>
      </div>

      {/* Audio Recording Dialog */}
      <Dialog open={isRecording} onOpenChange={setIsRecording}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Record Audio</DialogTitle>
          </DialogHeader>
          <AudioRecorder onRecordingComplete={handleRecordingComplete} onCancel={handleRecordingCancel} />
        </DialogContent>
      </Dialog>
    </>
  );
}
