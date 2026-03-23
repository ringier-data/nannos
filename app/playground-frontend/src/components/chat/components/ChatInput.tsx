import { useState, useRef, useEffect, type KeyboardEvent, type ChangeEvent } from 'react';
import { Send, AlertTriangle, Mic, X, Paperclip, Square } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useChat } from '../contexts';
import { useAuth } from '@/contexts/AuthContext';
import { AudioRecorder } from './AudioRecorder';
import { toast } from 'sonner';
import type { UploadedFileInfo, UploadedFileResponse } from '@/api/generated';

interface PendingFile {
  id: string;
  file: File;
  name: string;
  type: string;
  size: number;
  previewUrl?: string;
}

export function ChatInput() {
  const { sendMessage, isConnected, isWaiting, interruptTask, activeConversationId } = useChat();
  const { isImpersonating } = useAuth();
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
        const formData = new FormData();
        // If no active conversation yet, the backend will handle creating one
        // or the upload will fail with a helpful error message
        if (activeConversationId) {
          formData.append('conversation_id', activeConversationId);
        }
        
        pendingFiles.forEach((pendingFile) => {
          formData.append('files', pendingFile.file, pendingFile.name);
        });

        const response = await fetch('/api/v1/files/upload', {
          method: 'POST',
          body: formData,
          credentials: 'include',
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.detail || 'Upload failed');
        }

        const data: UploadedFileResponse = await response.json();
        console.log('Files uploaded:', data.files);

        // Store file data with both uri (for display) and s3Url (for storage)
        fileAttachments = data.files.map((file: UploadedFileInfo) => ({
          uri: file.uri,  // presigned URL for immediate display
          mimeType: file.mimeType,
          name: file.name,
          s3Url: file.s3Url,  // s3:// URL for storage and regeneration
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
    sendMessage(
      value.trim(), 
      fileAttachments.length > 0 ? fileAttachments : undefined
    );
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
      <div className="p-4 border-t border-border bg-card">
        <Alert variant="default" className="border-amber-500/50 bg-amber-500/10">
          <AlertTriangle className="h-4 w-4 text-amber-600" />
          <AlertDescription className="text-amber-600">
            Chat is unavailable while impersonating. Chat requires the user's access token which is not available during impersonation.
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <>
      <div 
        className={cn(
          "flex flex-col gap-2 p-4 border-t border-border bg-card relative",
          isDragging && "ring-2 ring-primary ring-offset-2"
        )}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        {/* Drag overlay */}
        {isDragging && (
          <div className="absolute inset-0 bg-primary/10 backdrop-blur-sm flex items-center justify-center z-10 rounded-lg">
            <div className="text-center">
              <Paperclip className="w-12 h-12 mx-auto mb-2 text-primary" />
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
                className="flex items-center gap-2 px-3 py-2 bg-muted rounded-md text-sm"
              >
                {file.previewUrl ? (
                  <img
                    src={file.previewUrl}
                    alt={file.name}
                    className="w-8 h-8 object-cover rounded"
                  />
                ) : file.type.startsWith('audio/') ? (
                  <Mic className="w-4 h-4 text-muted-foreground" />
                ) : (
                  <Paperclip className="w-4 h-4 text-muted-foreground" />
                )}
                <div className="flex flex-col min-w-0">
                  <span className="truncate max-w-[200px]">{file.name}</span>
                  <span className="text-xs text-muted-foreground">{formatFileSize(file.size)}</span>
                </div>
                <Button
                  size="icon"
                  variant="ghost"
                  className="h-6 w-6 flex-shrink-0"
                  onClick={() => removeFile(file.id)}
                >
                  <X className="w-4 h-4" />
                </Button>
              </div>
            ))}
          </div>
        )}

        <div className="flex gap-2">
          {/* File attachment button */}
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
            variant="outline"
            onClick={() => fileInputRef.current?.click()}
            disabled={!isConnected || isUploading}
            className="flex-shrink-0"
            aria-label="Attach file"
          >
            <Paperclip className="w-5 h-5" />
          </Button>

          {/* Microphone button */}
          <Button
            size="icon"
            variant="outline"
            onClick={() => setIsRecording(true)}
            disabled={!isConnected || isUploading}
            className="flex-shrink-0"
            aria-label="Record audio"
          >
            <Mic className="w-5 h-5" />
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
                : isConnected
                ? 'Type your message... (paste images or drag & drop files)'
                : 'Connect to an agent to start chatting...'
            }
            disabled={!isConnected || isUploading}
            rows={2}
            className={cn('flex-1 resize-none', 'transition-all duration-200')}
            data-testid="input-message"
          />
          {isWaiting ? (
            <Button
              onClick={interruptTask}
              size="icon"
              variant="destructive"
              className="flex-shrink-0 h-auto p-3"
              data-testid="button-stop"
              aria-label="Stop generation"
            >
              <Square className="w-4 h-4 fill-current" />
            </Button>
          ) : (
            <Button
              onClick={handleSend}
              disabled={!canSend}
              size="icon"
              className="flex-shrink-0 h-auto p-3"
              data-testid="button-send"
              aria-label="Send message"
            >
              <Send className="w-5 h-5" />
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
          <AudioRecorder
            onRecordingComplete={handleRecordingComplete}
            onCancel={handleRecordingCancel}
          />
        </DialogContent>
      </Dialog>
    </>
  );
}
