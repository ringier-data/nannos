import { useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { useChat } from '../contexts';

/** Human-readable labels for known HITL tool names. Falls back to raw name. */
const TOOL_LABELS: Record<string, string> = {
  console_create_bug_report: 'Bug Report',
  update_agents_md: 'Update Playbook (AGENTS.md)',
  create_skill_md: 'Create Skill',
  update_skill_md: 'Update Skill',
};

/** Keys whose values are long-form content (shown in a preview pane, not as metadata). */
const CONTENT_KEYS = new Set(['content', 'body', 'description']);

/** Keys that are internal / not useful for display. */
const HIDDEN_KEYS = new Set(['reason']);

export function InterruptConfirmCard() {
  const { pendingInterrupt, dismissInterrupt, sendSilentMessage } = useChat();
  const [feedback, setFeedback] = useState('');
  const [showFeedback, setShowFeedback] = useState(false);

  if (!pendingInterrupt) return null;

  const action = pendingInterrupt.actionRequests?.[0];
  const args = (action?.args || {}) as Record<string, unknown>;
  const toolLabel = TOOL_LABELS[pendingInterrupt.toolName] || pendingInterrupt.toolName;

  // Separate content fields from metadata fields
  const contentValue = [...CONTENT_KEYS.values()]
    .map((k: string) => args[k] as string | undefined)
    .find((v: string | undefined) => v);

  const metaEntries = Object.entries(args).filter(
    ([k]) => !CONTENT_KEYS.has(k) && !HIDDEN_KEYS.has(k)
  );

  // Determine allowed decisions from review_configs
  const reviewConfig = pendingInterrupt.reviewConfigs?.find(
    (rc) => rc.action_name === pendingInterrupt.toolName
  );
  const allowed = new Set(reviewConfig?.allowed_decisions ?? ['approve', 'reject']);

  const handleApprove = () => {
    sendSilentMessage('', [{ decisions: [{ type: 'approve' }] }]);
    dismissInterrupt();
    setFeedback('');
    setShowFeedback(false);
  };

  const handleReject = () => {
    sendSilentMessage('', [{ decisions: [{ type: 'reject', message: 'The user explicitly rejected this tool call via the human-in-the-loop approval. The tool was NOT executed. Do not retry or attempt workarounds unless the user explicitly asks.' }] }]);
    dismissInterrupt();
    setFeedback('');
    setShowFeedback(false);
  };

  const handleRequestChanges = () => {
    if (!feedback.trim()) return;
    const rejectMessage = `The user requested changes to this tool call. Please revise and try again.\n\nUser feedback: ${feedback.trim()}`;
    sendSilentMessage('', [{ decisions: [{ type: 'reject', message: rejectMessage }] }]);
    dismissInterrupt();
    setFeedback('');
    setShowFeedback(false);
  };

  return (
    <div className="mx-4 mb-3 rounded-lg border border-amber-500/30 bg-amber-50 dark:bg-amber-950/20 p-4 space-y-3">
      <div className="flex items-start gap-3">
        <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
        <div className="space-y-1 flex-1 min-w-0">
          <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
            {toolLabel}
          </p>
          {pendingInterrupt.reason && (
            <p className="text-sm text-amber-800 dark:text-amber-200">
              {pendingInterrupt.reason}
            </p>
          )}
          {metaEntries.length > 0 && (
            <div className="flex flex-wrap gap-2 text-xs text-amber-700 dark:text-amber-300">
              {metaEntries.map(([k, v]) => (
                <span key={k}>
                  {k}: <strong>{String(v)}</strong>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Show proposed content in a read-only preview pane */}
      {contentValue && (
        <div className="rounded border bg-white dark:bg-gray-900 p-2 max-h-48 overflow-y-auto">
          <pre className="text-xs whitespace-pre-wrap font-mono text-gray-700 dark:text-gray-300">
            {contentValue}
          </pre>
        </div>
      )}

      {/* Feedback textarea — shown when user clicks "Request Changes" */}
      {allowed.has('edit') && showFeedback && (
        <Textarea
          placeholder="Describe what should be changed (e.g. 'Make the description shorter' or 'Change scope to group')"
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          rows={2}
          className="resize-none text-sm"
          autoFocus
        />
      )}

      <div className="flex gap-2 justify-end">
        {allowed.has('reject') && (
          <Button variant="outline" size="sm" onClick={handleReject}>
            Reject
          </Button>
        )}
        {allowed.has('edit') && showFeedback ? (
          <Button size="sm" onClick={handleRequestChanges} disabled={!feedback.trim()}>
            Submit Feedback
          </Button>
        ) : (
          <>
            {allowed.has('edit') && (
              <Button variant="outline" size="sm" onClick={() => setShowFeedback(true)}>
                Request Changes
              </Button>
            )}
            {allowed.has('approve') && (
              <Button size="sm" onClick={handleApprove}>
                Approve
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
