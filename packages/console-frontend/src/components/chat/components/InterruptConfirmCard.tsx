import { useState } from 'react';
import { AlertTriangle, ShieldAlert, ShieldCheck, Check, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
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
const HIDDEN_KEYS = new Set(['reason', '_risk_metadata']);

/** Risk metadata attached by the dynamic risk scoring middleware. */
interface RiskMetadata {
  source: 'risk_score';
  score: number;
  threshold: number;
  matched_pattern: string | null;
  server_slug: string;
  tool_name: string;
}

type ActionRequest = { name: string; args?: Record<string, unknown>; description?: string };

/** A single HITL decision sent back to the server. */
interface Decision {
  /** Per-call id (echoed from the action_request) so the server aligns decisions by id. */
  id?: string;
  type: 'approve' | 'reject' | 'edit';
  message?: string;
  bypass?: boolean;
  bypass_all?: boolean;
  bypass_pattern?: string | null;
}

/** Get risk level label and color based on score. */
function getRiskLevel(score: number): { label: string; color: string; icon: typeof ShieldAlert } {
  if (score >= 0.9) return { label: 'Critical', color: 'text-red-600 dark:text-red-400', icon: ShieldAlert };
  if (score >= 0.8) return { label: 'High', color: 'text-orange-600 dark:text-orange-400', icon: ShieldAlert };
  if (score >= 0.6) return { label: 'Medium', color: 'text-amber-600 dark:text-amber-400', icon: ShieldAlert };
  return { label: 'Low', color: 'text-yellow-600 dark:text-yellow-400', icon: ShieldCheck };
}

function riskMetaOf(action: ActionRequest | undefined): RiskMetadata | undefined {
  const meta = (action?.args || {})._risk_metadata as RiskMetadata | undefined;
  return meta?.source === 'risk_score' ? meta : undefined;
}

/** Stable per-call id the server uses to align decisions (top-level args._call_id). */
function callIdOf(action: ActionRequest | undefined): string | undefined {
  return (action?.args || {})._call_id as string | undefined;
}

/** Attach the action_request's per-call id to a decision (no-op if absent). */
function withCallId(action: ActionRequest | undefined, decision: Decision): Decision {
  const callId = callIdOf(action);
  return callId ? { ...decision, id: callId } : decision;
}

/** Tool label + risk badge + arg metadata + content preview for one action_request. */
function ActionDetails({ action }: { action: ActionRequest }) {
  const args = (action.args || {}) as Record<string, unknown>;
  const toolLabel = TOOL_LABELS[action.name] || action.name;
  const riskMeta = riskMetaOf(action);
  const riskLevel = riskMeta ? getRiskLevel(riskMeta.score) : null;

  const contentValue = [...CONTENT_KEYS.values()]
    .map((k) => args[k] as string | undefined)
    .find((v) => v);
  const metaEntries = Object.entries(args).filter(([k]) => !CONTENT_KEYS.has(k) && !HIDDEN_KEYS.has(k));

  return (
    <div className="space-y-1 min-w-0">
      <p className="text-sm font-medium text-amber-900 dark:text-amber-100">{toolLabel}</p>
      {action.description && (
        <p className="text-sm text-amber-800 dark:text-amber-200">{action.description}</p>
      )}
      {riskMeta && riskLevel && (
        <div className="flex items-center gap-2 text-xs">
          <span className={`font-medium ${riskLevel.color}`}>
            Risk: {riskLevel.label} ({Math.round(riskMeta.score * 100)}%)
          </span>
          {riskMeta.matched_pattern && (
            <span className="text-amber-700 dark:text-amber-300">
              — matched:{' '}
              <code className="bg-amber-100 dark:bg-amber-900/40 px-1 rounded">{riskMeta.matched_pattern}</code>
            </span>
          )}
        </div>
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
      {contentValue && (
        <div className="rounded border bg-white dark:bg-gray-900 p-2 max-h-48 overflow-y-auto">
          <pre className="text-xs whitespace-pre-wrap font-mono text-gray-700 dark:text-gray-300">
            {contentValue}
          </pre>
        </div>
      )}
    </div>
  );
}

/**
 * Single-action approval card — the rich, one-click experience (approve / bypass
 * variants / reject / request changes). Behaviour is unchanged from the original
 * card except each decision now echoes the action's call_id when present.
 */
function SingleActionCard() {
  const { pendingInterrupt, dismissInterrupt, sendSilentMessage } = useChat();
  const [feedback, setFeedback] = useState('');
  const [showFeedback, setShowFeedback] = useState(false);

  const action = pendingInterrupt!.actionRequests?.[0];
  const riskMeta = riskMetaOf(action);
  const isRiskScored = !!riskMeta;

  const reviewConfig = pendingInterrupt!.reviewConfigs?.find((rc) => rc.action_name === pendingInterrupt!.toolName);
  const allowed = new Set(reviewConfig?.allowed_decisions ?? ['approve', 'reject']);

  const send = (decision: Decision) => {
    sendSilentMessage('', [{ decisions: [withCallId(action, decision)] }]);
    dismissInterrupt();
    setFeedback('');
    setShowFeedback(false);
  };

  const riskLevel = isRiskScored ? getRiskLevel(riskMeta!.score) : null;
  const RiskIcon = riskLevel?.icon ?? AlertTriangle;

  return (
    <div className="mx-4 mb-3 rounded-lg border border-amber-500/30 bg-amber-50 dark:bg-amber-950/20 p-4 space-y-3">
      <div className="flex items-start gap-3">
        <RiskIcon className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          {pendingInterrupt!.reason && (
            <p className="text-sm text-amber-800 dark:text-amber-200 mb-1">{pendingInterrupt!.reason}</p>
          )}
          {action && <ActionDetails action={action} />}
        </div>
      </div>

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

      <div className="flex gap-2 justify-end flex-wrap">
        {allowed.has('reject') && (
          <Button variant="outline" size="sm" onClick={() => send({ type: 'reject' })}>
            Reject
          </Button>
        )}
        {allowed.has('edit') && showFeedback ? (
          <Button
            size="sm"
            onClick={() =>
              feedback.trim() &&
              send({
                type: 'reject',
                message: `The user requested changes to this tool call. Please revise and try again.\n\nUser feedback: ${feedback.trim()}`,
              })
            }
            disabled={!feedback.trim()}
          >
            Submit Feedback
          </Button>
        ) : (
          <>
            {allowed.has('edit') && (
              <Button variant="outline" size="sm" onClick={() => setShowFeedback(true)}>
                Request Changes
              </Button>
            )}
            {isRiskScored && allowed.has('approve') && (
              <>
                {riskMeta!.matched_pattern && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => send({ type: 'approve', bypass: true, bypass_pattern: riskMeta!.matched_pattern })}
                      >
                        Allow Pattern
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Approve and skip this specific pattern next time</TooltipContent>
                  </Tooltip>
                )}
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => send({ type: 'approve', bypass: true, bypass_all: true })}
                    >
                      Always Allow
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Approve and never ask again for this tool</TooltipContent>
                </Tooltip>
              </>
            )}
            {allowed.has('approve') && (
              <Button size="sm" onClick={() => send({ type: 'approve' })}>
                Approve
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Multi-action approval card — one interrupt carrying N action_requests (e.g. two
 * high-risk eval calls, or parallel tool calls). Renders every call, collects one
 * decision per call, and submits a SINGLE batched response (the suspended graph
 * resumes once). Decisions echo each call_id so the server aligns them by id.
 */
function MultiActionCard() {
  const { pendingInterrupt, dismissInterrupt, sendSilentMessage } = useChat();
  const actions = (pendingInterrupt!.actionRequests ?? []) as ActionRequest[];

  const [choices, setChoices] = useState<Record<number, 'approve' | 'reject'>>({});
  const [messages, setMessages] = useState<Record<number, string>>({});

  const decidedCount = Object.keys(choices).length;
  const allDecided = decidedCount === actions.length;

  const submit = (perIdx: (idx: number) => 'approve' | 'reject') => {
    const decisions: Decision[] = actions.map((action, idx) => {
      const type = perIdx(idx);
      // Plain reject → no message; the server supplies the default. Only attach a
      // message when the user typed a per-call reason.
      const note = messages[idx]?.trim();
      const base: Decision = type === 'reject' && note ? { type, message: note } : { type };
      return withCallId(action, base);
    });
    sendSilentMessage('', [{ decisions }]);
    dismissInterrupt();
  };

  return (
    <div className="mx-4 mb-3 rounded-lg border border-amber-500/30 bg-amber-50 dark:bg-amber-950/20 p-4 space-y-3">
      <div className="flex items-center gap-3">
        <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0" />
        <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
          {actions.length} actions need your approval
        </p>
        <span className="text-xs text-amber-700 dark:text-amber-300 ml-auto">
          {decidedCount}/{actions.length} decided
        </span>
      </div>

      <div className="space-y-2">
        {actions.map((action, idx) => {
          const choice = choices[idx];
          return (
            <div
              key={callIdOf(action) ?? idx}
              className="rounded border border-amber-500/20 bg-white/50 dark:bg-gray-900/30 p-3 space-y-2"
            >
              <div className="flex items-start gap-2">
                <div className="flex-1 min-w-0">
                  <ActionDetails action={action} />
                </div>
                <div className="flex gap-1 shrink-0">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant={choice === 'approve' ? 'default' : 'outline'}
                        size="sm"
                        onClick={() => setChoices((p) => ({ ...p, [idx]: 'approve' }))}
                      >
                        <Check className="w-4 h-4" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Approve this call</TooltipContent>
                  </Tooltip>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant={choice === 'reject' ? 'destructive' : 'outline'}
                        size="sm"
                        onClick={() => setChoices((p) => ({ ...p, [idx]: 'reject' }))}
                      >
                        <X className="w-4 h-4" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Reject this call</TooltipContent>
                  </Tooltip>
                </div>
              </div>
              {choice === 'reject' && (
                <Textarea
                  placeholder="Optional: why is this rejected? (sent to the agent)"
                  value={messages[idx] ?? ''}
                  onChange={(e) => setMessages((p) => ({ ...p, [idx]: e.target.value }))}
                  rows={1}
                  className="resize-none text-xs"
                />
              )}
            </div>
          );
        })}
      </div>

      <div className="flex gap-2 justify-end flex-wrap">
        <Button variant="outline" size="sm" onClick={() => submit(() => 'reject')}>
          Reject all
        </Button>
        <Button variant="outline" size="sm" onClick={() => submit(() => 'approve')}>
          Approve all
        </Button>
        <Button size="sm" onClick={() => submit((idx) => choices[idx])} disabled={!allDecided}>
          Submit decisions
        </Button>
      </div>
    </div>
  );
}

export function InterruptConfirmCard() {
  const { pendingInterrupt } = useChat();
  if (!pendingInterrupt) return null;
  const actions = pendingInterrupt.actionRequests ?? [];
  return actions.length > 1 ? <MultiActionCard /> : <SingleActionCard />;
}
