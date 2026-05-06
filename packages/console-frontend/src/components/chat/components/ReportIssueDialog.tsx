import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
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
import {
  createBugReportApiV1BugReportsPostMutation,
} from '@/api/generated/@tanstack/react-query.gen';

interface ReportIssueDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId: string;
  messageId?: string;
}

export function ReportIssueDialog({ open, onOpenChange, conversationId, messageId }: ReportIssueDialogProps) {
  const [description, setDescription] = useState('');

  const mutation = useMutation({
    ...createBugReportApiV1BugReportsPostMutation(),
    onSuccess: () => {
      toast.success('Issue reported successfully');
      setDescription('');
      onOpenChange(false);
    },
    onError: () => {
      toast.error('Failed to report issue');
    },
  });

  const handleSubmit = () => {
    mutation.mutate({
      body: {
        conversation_id: conversationId,
        message_id: messageId,
        description: description || undefined,
        source: 'client',
      },
    });
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
          <Button onClick={handleSubmit} disabled={mutation.isPending}>
            {mutation.isPending ? 'Submitting...' : 'Submit Report'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
