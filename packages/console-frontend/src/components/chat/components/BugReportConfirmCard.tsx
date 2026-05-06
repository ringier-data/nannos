import { useState } from 'react';
import { Bug } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { useChat } from '../contexts';

export function BugReportConfirmCard() {
  const { pendingBugReport, dismissBugReport, sendSilentMessage } = useChat();
  const [description, setDescription] = useState('');

  if (!pendingBugReport) return null;

  const handleConfirm = () => {
    // Send HumanInTheLoopMiddleware decisions as structured DataPart to orchestrator.
    // The MCP tool (console_create_bug_report) handles actual creation after approval.
    if (description) {
      const originalAction = pendingBugReport.actionRequests?.[0];
      const editedArgs = { ...(originalAction?.args || {}), description };
      sendSilentMessage('', [{ decisions: [{ type: 'edit', edited_action: { name: originalAction?.name || 'console_create_bug_report', args: editedArgs } }] }]);
    } else {
      sendSilentMessage('', [{ decisions: [{ type: 'approve' }] }]);
    }
    dismissBugReport();
    setDescription('');
  };

  const handleDecline = () => {
    sendSilentMessage('', [{ decisions: [{ type: 'reject', message: 'User declined' }] }]);
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
        <Button size="sm" onClick={handleConfirm}>
          Report Issue
        </Button>
      </div>
    </div>
  );
}
