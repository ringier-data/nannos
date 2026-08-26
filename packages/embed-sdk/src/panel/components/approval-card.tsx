/**
 * The HITL approval card: one section per pending tool approval (stacked when
 * several arrive in a batch). Each section shows the tool name, a risk badge
 * derived from `_risk_metadata`, the tool args as a scrollable field→value
 * table, and the decision buttons gated by the interrupt's review configs.
 * Reject/edit decisions ride the versioned reason envelope the transport's
 * approval codec understands. Built on the vendored ai-elements
 * `confirmation.tsx` shell.
 */
import { useState } from 'react';
import { MessageSquarePlusIcon, ShieldAlertIcon } from 'lucide-react';
import {
  Confirmation,
  ConfirmationAction,
  ConfirmationActions,
  ConfirmationTitle,
} from '../../components/ai-elements/confirmation';
import { Textarea } from '../../components/ui/textarea';
import { cn } from '../../lib/utils';
import { format, useStrings } from '../../react';
import type { NannosStrings } from '../../i18n/keys';
import type { PendingApproval, UseNannosChatValue } from '../hooks/use-nannos-chat';

export interface ApprovalCardProps {
  interrupt: UseNannosChatValue['interrupt'];
  className?: string;
}

const HIDDEN_ARG_KEYS = new Set(['reason', '_risk_metadata', '_call_id', '_summary']);

interface RiskInfo {
  levelKey: keyof NannosStrings;
  score: number;
  textClass: string;
}

function readRisk(input: Record<string, unknown>): RiskInfo | null {
  const raw = input._risk_metadata;
  if (typeof raw !== 'object' || raw === null) return null;
  const meta = raw as { source?: unknown; score?: unknown };
  if (meta.source !== 'risk_score' || typeof meta.score !== 'number') return null;
  const score = meta.score;
  if (score >= 0.9) return { levelKey: 'hitl.riskCritical', score, textClass: 'text-destructive' };
  if (score >= 0.8) return { levelKey: 'hitl.riskHigh', score, textClass: 'text-destructive' };
  if (score >= 0.6)
    return { levelKey: 'hitl.riskMedium', score, textClass: 'text-amber-600 dark:text-amber-500' };
  return { levelKey: 'hitl.riskLow', score, textClass: 'text-muted-foreground' };
}

/** The rows to show: `client_action` renders its directive values; other tools their args. */
function argRows(approval: PendingApproval): Array<[string, unknown]> {
  if (approval.toolName === 'client_action') {
    const directive = approval.input.directive;
    if (typeof directive === 'object' && directive !== null) {
      const values = (directive as { values?: unknown }).values;
      if (typeof values === 'object' && values !== null) {
        return Object.entries(values as Record<string, unknown>);
      }
    }
    return [];
  }
  return Object.entries(approval.input).filter(([key]) => !HIDDEN_ARG_KEYS.has(key));
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined) return '';
  return JSON.stringify(value);
}

function reasonEnvelope(type: 'reject' | 'edit', message: string): string {
  const trimmed = message.trim();
  return JSON.stringify({ v: 1, type, ...(trimmed && { message: trimmed }) });
}

function ApprovalSection({
  approval,
  interrupt,
}: {
  approval: PendingApproval;
  interrupt: UseNannosChatValue['interrupt'];
}) {
  const strings = useStrings();
  const [message, setMessage] = useState('');
  const [showMessage, setShowMessage] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const allowedDecisions =
    interrupt.reviewConfigs.find((rc) => rc.action_name === approval.toolName)
      ?.allowed_decisions ?? ['approve', 'reject'];
  const canApprove = allowedDecisions.includes('approve');
  const canReject = allowedDecisions.includes('reject');
  const canEdit = allowedDecisions.includes('edit');

  const risk = readRisk(approval.input);
  const rows = argRows(approval);
  const summary = typeof approval.input._summary === 'string' ? approval.input._summary : null;

  const decide = (approved: boolean, reason?: string) => {
    setSubmitting(true);
    void interrupt.respond(approval.approvalId, approved, reason).finally(() => setSubmitting(false));
  };

  return (
    <Confirmation
      data-slot="nannos-approval-action"
      approval={{ id: approval.approvalId }}
      state="approval-requested"
    >
      <ConfirmationTitle className="flex min-w-0 items-baseline gap-1.5">
        <span className="shrink-0 font-bold text-xs">{approval.toolName}</span>
        {rows.length === 1 && (
          <span
            className="min-w-0 truncate text-muted-foreground text-xs"
            title={`${rows[0][0]}: ${formatValue(rows[0][1])}`}
          >
            {rows[0][0]}: {formatValue(rows[0][1])}
          </span>
        )}
      </ConfirmationTitle>

      {summary && (
        <p data-slot="nannos-approval-summary" className="text-foreground/80 text-xs">
          {summary}
        </p>
      )}

      {rows.length > 1 && (
        <div className="overflow-x-auto rounded-md bg-muted/50">
          <table className="w-full text-xs">
            <tbody>
              {rows.map(([key, value]) => (
                <tr key={key} className="border-b last:border-b-0">
                  <td className="whitespace-nowrap px-2 py-1 align-top font-medium text-muted-foreground">
                    {key}
                  </td>
                  <td className="px-2 py-1">
                    <pre className="whitespace-pre-wrap break-words font-sans">
                      {formatValue(value)}
                    </pre>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(canReject || canEdit) && showMessage && (
        <Textarea
          data-slot="nannos-approval-message"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder={strings['hitl.reasonPlaceholder']}
          rows={1}
          autoFocus
          className="min-h-0 px-2 py-1.5 pointer-fine:text-xs"
          disabled={submitting}
        />
      )}

      <ConfirmationActions
        data-slot="nannos-approval-actions"
        className="justify-start gap-1.5 self-stretch"
      >
        {canApprove && (
          <ConfirmationAction
            variant="outline"
            data-slot="nannos-approval-approve"
            disabled={submitting}
            onClick={() => decide(true)}
          >
            {strings['hitl.approve']}
          </ConfirmationAction>
        )}
        {canReject && (
          <ConfirmationAction
            data-slot="nannos-approval-reject"
            variant="ghost"
            disabled={submitting}
            onClick={() => decide(false, reasonEnvelope('reject', message))}
          >
            {strings['hitl.reject']}
          </ConfirmationAction>
        )}
        {canEdit && (
          <ConfirmationAction
            data-slot="nannos-approval-edit"
            variant="secondary"
            disabled={submitting || !message.trim()}
            onClick={() => decide(false, reasonEnvelope('edit', message))}
          >
            {strings['hitl.requestChanges']}
          </ConfirmationAction>
        )}
        {(canReject || canEdit) && (
          <ConfirmationAction
            data-slot="nannos-approval-message-toggle"
            variant="ghost"
            size="icon"
            className="size-6"
            aria-label={strings['hitl.reasonPlaceholder']}
            title={strings['hitl.reasonPlaceholder']}
            disabled={submitting}
            onClick={() => setShowMessage((open) => !open)}
          >
            <MessageSquarePlusIcon className="size-3.5" />
          </ConfirmationAction>
        )}
        {risk && (
          <span className={cn('ml-auto text-[11px]', risk.textClass)}>
            {format(strings['hitl.risk'], {
              level: strings[risk.levelKey],
              percent: Math.round(risk.score * 100),
            })}
          </span>
        )}
      </ConfirmationActions>
    </Confirmation>
  );
}

export function ApprovalCard({ interrupt, className }: ApprovalCardProps) {
  const strings = useStrings();
  const [submittingAll, setSubmittingAll] = useState(false);
  if (interrupt.pending.length === 0) return null;

  const everyoneAllows = (decision: string) =>
    interrupt.pending.every((approval) =>
      (
        interrupt.reviewConfigs.find((rc) => rc.action_name === approval.toolName)
          ?.allowed_decisions ?? ['approve', 'reject']
      ).includes(decision),
    );
  const canApproveAll = everyoneAllows('approve');
  const canRejectAll = everyoneAllows('reject');

  const decideAll = (approved: boolean) => {
    setSubmittingAll(true);
    void Promise.all(
      interrupt.pending.map((approval) =>
        interrupt.respond(
          approval.approvalId,
          approved,
          approved ? undefined : reasonEnvelope('reject', ''),
        ),
      ),
    ).finally(() => setSubmittingAll(false));
  };

  const showBatchActions = interrupt.pending.length > 1;

  return (
    <div
      data-slot="nannos-approval-card"
      className={cn('space-y-1.5 rounded-lg border bg-card p-2.5 text-card-foreground', className)}
    >
      <div className="flex items-center gap-1.5 font-medium text-xs">
        <ShieldAlertIcon aria-hidden="true" className="size-3.5 text-amber-600" />
        {strings['hitl.title']}
      </div>
      {interrupt.reason && (
        <p className="text-muted-foreground text-xs">{interrupt.reason}</p>
      )}
      {interrupt.pending.map((approval) => (
        <ApprovalSection key={approval.toolCallId} approval={approval} interrupt={interrupt} />
      ))}
      {showBatchActions && (
        <div data-slot="nannos-approval-batch-actions" className="flex items-center gap-1.5">
          {canApproveAll && (
            <ConfirmationAction
              data-slot="nannos-approval-approve-all"
              disabled={submittingAll}
              onClick={() => decideAll(true)}
            >
              {strings['hitl.approveAll']}
            </ConfirmationAction>
          )}
          {canRejectAll && (
            <ConfirmationAction
              data-slot="nannos-approval-reject-all"
              variant="outline"
              disabled={submittingAll}
              onClick={() => decideAll(false)}
            >
              {strings['hitl.rejectAll']}
            </ConfirmationAction>
          )}
        </div>
      )}
    </div>
  );
}
