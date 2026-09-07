/**
 * The HITL approval card: one section per pending tool approval (stacked when
 * several arrive in a batch). Each section shows the tool name, a risk badge
 * derived from `_risk_metadata`, the tool args as a scrollable field→value
 * table, and the decision buttons gated by the interrupt's review configs.
 * A `client_action` `apply` whose target is registered upgrades that table to
 * a field → current → new diff, so the user sees what the fill would change.
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
import { CLIENT_ACTION_TOOL, clientActionSummaryKey, toolPartTitle } from '../tool-title';
import { InterruptActions, InterruptCard } from './interrupt-card';
import { useChatEngineOptional } from '../engine';
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

/** The directive values behind a `client_action`, whichever shape carried them:
 *  the nested wire directive (`{ directive: { values } }`) or the flat
 *  snake_case tool args the risk-gate approval surfaces (`{ values }`). */
function clientActionValues(input: Record<string, unknown>): Record<string, unknown> | null {
  const directive = input.directive;
  const values =
    typeof directive === 'object' && directive !== null
      ? (directive as { values?: unknown }).values
      : input.values;
  return typeof values === 'object' && values !== null ? (values as Record<string, unknown>) : null;
}

/** The rows to show: `client_action` renders its directive values; other tools their args. */
function argRows(approval: PendingApproval): Array<[string, unknown]> {
  if (approval.toolName === CLIENT_ACTION_TOOL) {
    const values = clientActionValues(approval.input);
    return values ? Object.entries(values) : [];
  }
  return Object.entries(approval.input).filter(([key]) => !HIDDEN_ARG_KEYS.has(key));
}

/** The `apply` target behind a `client_action` approval, whichever shape it took:
 *  nested (`{ directive: { kind, target: { type, id } } }`) or the flat risk-gate
 *  args (`{ kind, target_type, target_id }`). Null for every other kind. */
function applyTarget(input: Record<string, unknown>): { type: string; id: string } | null {
  const directive = input.directive;
  if (typeof directive === 'object' && directive !== null) {
    const d = directive as { kind?: unknown; target?: { type?: unknown; id?: unknown } };
    if (d.kind !== 'apply') return null;
    return typeof d.target?.type === 'string' && typeof d.target?.id === 'string'
      ? { type: d.target.type, id: d.target.id }
      : null;
  }
  if (input.kind !== 'apply') return null;
  return typeof input.target_type === 'string' && typeof input.target_id === 'string'
    ? { type: input.target_type, id: input.target_id }
    : null;
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

/** Cell text for the apply diff: empty-ish values ('' / null / undefined) all
 *  read as "no value" so the em-dash placeholder can stand in for them. */
function diffText(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  return formatValue(value);
}

/** Field → current → new, one row per field the agent wants to write. Current
 *  values come from the registered target's `getState()` at render time — the
 *  form as the user sees it while deciding. Changed rows tint the pair's cell
 *  backgrounds diff-style (current red, new green); unchanged rows stay muted. */
function ApplyDiffTable({
  rows,
  current,
}: {
  rows: Array<[string, unknown]>;
  current: Record<string, unknown>;
}) {
  const strings = useStrings();
  return (
    <div data-slot="nannos-approval-diff" className="overflow-x-auto rounded-md bg-muted/50">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b text-left text-muted-foreground">
            <th className="px-2 py-1 font-medium">{strings['hitl.diff.field']}</th>
            <th className="px-2 py-1 font-medium">{strings['hitl.diff.current']}</th>
            <th className="px-2 py-1 font-medium">{strings['hitl.diff.new']}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([key, value]) => {
            const before = diffText(current[key]);
            const after = diffText(value);
            const changed = before !== after;
            return (
              <tr key={key} className="border-b last:border-b-0">
                <td className="whitespace-nowrap px-2 py-1 align-top font-medium text-muted-foreground">
                  {key}
                </td>
                <td
                  className={cn(
                    'px-2 py-1 align-top',
                    changed ? 'bg-destructive/10' : 'text-muted-foreground',
                  )}
                >
                  <pre className="whitespace-pre-wrap break-words font-sans">{before || '—'}</pre>
                </td>
                <td
                  className={cn(
                    'px-2 py-1 align-top',
                    changed ? 'bg-emerald-500/15 dark:bg-emerald-500/20' : 'text-muted-foreground',
                  )}
                >
                  <pre className="whitespace-pre-wrap break-words font-sans">{after || '—'}</pre>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ApprovalSection({
  approval,
  interrupt,
  divided,
}: {
  approval: PendingApproval;
  interrupt: UseNannosChatValue['interrupt'];
  /** Rule above — every section after the first in a batch. */
  divided?: boolean;
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
  // An `apply` shows a field diff when its target is registered: current values
  // straight from the handle's getState(). Unregistered target (or a throwing
  // getState mid-render) → null → the plain args table below.
  const engine = useChatEngineOptional();
  const target = approval.toolName === CLIENT_ACTION_TOOL ? applyTarget(approval.input) : null;
  let currentState: Record<string, unknown> | null = null;
  if (target && engine && rows.length > 0) {
    try {
      const state = engine.core.registry.get(target.type, target.id)?.getState();
      if (typeof state === 'object' && state !== null)
        currentState = state as Record<string, unknown>;
    } catch {
      /* fall back to the plain table */
    }
  }
  // `_summary` is the agent's plain-language sentence. `client_action` no longer
  // gets one — its `kind` is a closed enum, so the SDK describes it from its own
  // strings and the user reads it in their own language.
  const clientActionKey =
    approval.toolName === CLIENT_ACTION_TOOL ? clientActionSummaryKey(approval.input) : null;
  const summary =
    typeof approval.input._summary === 'string'
      ? approval.input._summary
      : clientActionKey
        ? strings[clientActionKey]
        : null;

  const decide = (approved: boolean, reason?: string) => {
    setSubmitting(true);
    void interrupt.respond(approval.approvalId, approved, reason).finally(() => setSubmitting(false));
  };

  return (
    <Confirmation
      data-slot="nannos-approval-action"
      approval={{ id: approval.approvalId }}
      state="approval-requested"
      className={cn(divided && 'border-t')}
    >
      <ConfirmationTitle className="flex min-w-0 items-baseline gap-1.5">
        <span className="shrink-0 font-bold text-xs">
          {toolPartTitle(approval.toolName, approval.input)}{summary && (`: ${summary}`)}
        </span>
        {rows.length === 1 && !currentState && (
          <span
            className="min-w-0 truncate text-muted-foreground text-xs"
            title={`${rows[0][0]}: ${formatValue(rows[0][1])}`}
          >
            {rows[0][0]}: {formatValue(rows[0][1])}
          </span>
        )}
      </ConfirmationTitle>

      {currentState ? (
        <ApplyDiffTable rows={rows} current={currentState} />
      ) : (
        rows.length > 1 && (
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
        )
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
    <InterruptCard
      slot="nannos-approval-card"
      className={className}
      icon={<ShieldAlertIcon aria-hidden="true" className="size-3.5 shrink-0 text-amber-600" />}
      // A batch COUNTS rather than naming: three concatenated tool titles
      // truncate to nothing useful in a 400px panel, and the sections below
      // name each one anyway.
      title={
        showBatchActions
          ? format(strings['hitl.titleCount'], { count: String(interrupt.pending.length) })
          : format(strings['hitl.title'], {
              toolName: toolPartTitle(interrupt.pending[0].toolName, interrupt.pending[0].input),
            })
      }
    >
      {interrupt.pending.map((approval, index) => (
        <ApprovalSection
          key={approval.toolCallId}
          approval={approval}
          interrupt={interrupt}
          divided={index > 0}
        />
      ))}
      {showBatchActions && (
        <InterruptActions slot="nannos-approval-batch-actions">
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
        </InterruptActions>
      )}
    </InterruptCard>
  );
}
