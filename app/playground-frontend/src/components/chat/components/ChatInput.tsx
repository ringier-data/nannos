import { useState, useRef, useEffect, type KeyboardEvent, type ChangeEvent } from 'react';
import { Send, AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { useChat } from '../contexts';
import { useAuth } from '@/contexts/AuthContext';

export function ChatInput() {
  const { sendMessage, isConnected } = useChat();
  const { isImpersonating } = useAuth();
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const canSend = isConnected && value.trim().length > 0;

  const handleSend = () => {
    if (!canSend) return;
    sendMessage(value.trim());
    setValue('');
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
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
    <div className="flex gap-3 p-4 border-t border-border bg-card">
      <Textarea
        ref={textareaRef}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={isConnected ? 'Type your message...' : 'Connect to an agent to start chatting...'}
        disabled={!isConnected}
        rows={2}
        className={cn('flex-1 resize-none', 'transition-all duration-200')}
        data-testid="input-message"
      />
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
    </div>
  );
}
