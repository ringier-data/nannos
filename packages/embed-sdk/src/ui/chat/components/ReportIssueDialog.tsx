import { useState } from 'react';
import { toast } from 'sonner';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { useHostAdapter } from '../../adapter';

interface ReportIssueDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId: string;
  messageId?: string;
}

/**
 * Bug-report dialog. Requires `adapter.api.reportIssue` — callers gate the
 * trigger on its presence (hosts without a bug-report backend hide the flag).
 */
export function ReportIssueDialog({ open, onOpenChange, conversationId, messageId }: ReportIssueDialogProps) {
  const { api } = useHostAdapter();
  const [description, setDescription] = useState('');
  const [isPending, setIsPending] = useState(false);

  const handleSubmit = async () => {
    if (!api.reportIssue) return;
    setIsPending(true);
    try {
      const ok = await api.reportIssue({
        conversationId,
        messageId,
        description: description || undefined,
      });
      if (ok) {
        toast.success('Issue reported successfully');
        setDescription('');
        onOpenChange(false);
      } else {
        toast.error('Failed to report issue');
      }
    } catch {
      toast.error('Failed to report issue');
    } finally {
      setIsPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Report Issue</DialogTitle>
          <DialogDescription>
            Describe the problem you encountered. Our team will investigate using the conversation context.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          placeholder="What went wrong?"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={4}
          className="resize-none"
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => void handleSubmit()} disabled={isPending}>
            {isPending ? 'Submitting...' : 'Submit Report'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
