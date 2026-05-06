import { useState } from 'react';
import { Bug } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  createBugReportApiV1BugReportsPostMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { useChat } from '../contexts';

export function BugReportConfirmCard() {
  const { pendingBugReport, dismissBugReport, sendMessage } = useChat();
  const [description, setDescription] = useState('');

  const mutation = useMutation({
    ...createBugReportApiV1BugReportsPostMutation(),
    onSuccess: () => {
      toast.success('Bug report submitted');
      // Send confirmation back to the orchestrator so it can resume
      sendMessage(description || 'confirmed');
      dismissBugReport();
      setDescription('');
    },
    onError: () => {
      toast.error('Failed to submit bug report');
    },
  });

  if (!pendingBugReport) return null;

  const handleConfirm = () => {
    mutation.mutate({
      body: {
        conversation_id: pendingBugReport.conversationId,
        task_id: pendingBugReport.taskId || undefined,
        description: description || pendingBugReport.reason || undefined,
        source: 'orchestrator',
      },
    });
  };

  const handleDecline = () => {
    // Send decline back to the orchestrator
    sendMessage('decline');
    dismissBugReport();
  };

  return (
    <div className="mx-4 mb-3 rounded-lg border border-amber-500/30 bg-amber-50 dark:bg-amber-950/20 p-4 space-y-3">
      <div className="flex items-start gap-3">
        <Bug className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
        <div className="space-y-1 flex-1 min-w-0">
          <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
            The agent encountered an issue it couldn&apos;t resolve
          </p>
          {pendingBugReport.reason && (
            <p className="text-sm text-amber-800 dark:text-amber-200">
              {pendingBugReport.reason}
            </p>
          )}
        </div>
      </div>
      <Textarea
        placeholder="Add any additional details (optional)"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        rows={2}
        className="resize-none text-sm"
      />
      <div className="flex gap-2 justify-end">
        <Button variant="outline" size="sm" onClick={handleDecline}>
          Dismiss
        </Button>
        <Button size="sm" onClick={handleConfirm} disabled={mutation.isPending}>
          {mutation.isPending ? 'Submitting...' : 'Report Issue'}
        </Button>
      </div>
    </div>
  );
}
