/**
 * The secondary-authorization prompt: shown when a tool the assistant wanted to
 * use needs the end-user's consent (the MCP gateway's `need-credentials`,
 * surfaced as A2A's own `auth-required` task state).
 *
 * It renders in the shared interrupt shell, beside the approval card and with
 * the same indent, head and type ramp — the user is being stopped for the same
 * reason, so it should not look like a different species of thing. What it is
 * NOT is an approval: nothing is being proposed here, so there is nothing for
 * the GATEWAY to refuse. The refusal is still real, though — it is addressed to
 * the AGENT, which is holding a blocked tool call and needs to be told to stop
 * asking. Both buttons therefore answer the same parked interrupt with an
 * explicit decision (a DataPart), and both resume the turn.
 *
 * The card walks the detour in place: "Authorize" opens the provider in a new
 * window, and once it is open the card switches to "Done, continue" — the click
 * that tells the agent to try again — with a ghost "Re-authorize" beside it for
 * the attempt that did not take.
 *
 * The copy is OURS and localized. The wire message that comes with the status
 * is the gateway addressing the agent ("You must tell the end-user to…") —
 * never end-user copy — so it is rendered only when we could not find a URL to
 * link (better stranded text than no way forward), and in dev mode. The turn
 * the confirm button sends is agent-facing too, so it stays English and out of
 * the localized strings; the user sees a receipt instead.
 */
import { useState } from 'react';
import { CheckIcon, ExternalLinkIcon, ShieldCheckIcon } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { format, useStrings } from '../../react';
import { useDevMode } from '../dev-mode';
import type { UseNannosChatValue } from '../hooks/use-nannos-chat';
import { InterruptActions, InterruptCard, InterruptSection } from './interrupt-card';
import { receiptHeadline } from './receipt';

export interface AuthRequiredCardProps {
  data: { authUrl?: string; tool?: string; service?: string; message?: string };
  /** The thread's `chat.send`. Without it the confirm button is not offered. */
  send?: UseNannosChatValue['send'];
  className?: string;
}

/**
 * Tool names that mean nothing to an end-user: sandbox and orchestration
 * plumbing. The gateway call that needs authorization often runs INSIDE one of
 * these (a `need-credentials` from an MCP call made in the sandbox is reported
 * against `eval`), so naming them would misinform rather than inform. Anything
 * else is shown verbatim — never reformatted, since a mangled tool name is
 * worse than a plain one.
 */
const OPAQUE_TOOLS = new Set([
  'eval',
  'task',
  'client_action',
  'python',
  'bash',
  'shell',
  'call_tool',
]);

/**
 * Compact sizing for the card's buttons: this is a footnote inside a turn, not
 * a dialog, so it stays smaller than the thread's own text.
 */
const ACTION_CLASS = 'h-6 gap-1 px-2 text-xs has-[>svg]:px-2 [&_svg]:size-3';

/** What the buttons actually send: agent-facing, so English and unlocalized.
 *  These are the fallback for a client that never negotiated the extension —
 *  the DataPart beside them is what the middleware acts on when it did. */
const CONTINUE_PROMPT = 'I have completed the authorization. Please retry what needed it and continue.';
const CONTINUE_PROMPT_TOOL =
  'I have completed the authorization for {tool}. Please retry what needed it and continue.';
const DECLINE_PROMPT =
  'I am not going to authorize this. Do not ask again — tell me what you cannot do without it.';
const DECLINE_PROMPT_TOOL =
  'I am not going to authorize {tool}. Do not ask again — tell me what you cannot do without it.';

export function AuthRequiredCard({ data, send, className }: AuthRequiredCardProps) {
  const strings = useStrings();
  const devMode = useDevMode();
  const tool = data.tool && !OPAQUE_TOOLS.has(data.tool) ? data.tool : undefined;
  // The same filter applies to the service: a producer that fills it in from the
  // tool name would otherwise slip `eval` past the check that exists precisely
  // to keep sandbox plumbing out of the user's face.
  const service = data.service && !OPAQUE_TOOLS.has(data.service) ? data.service : undefined;
  // What to call the thing being authorized: the service the credential belongs
  // to when the payload named one, else the tool that asked for it — and
  // nothing at all when neither is fit to show.
  const subject = service || tool || '';

  // `idle` until the user has been sent to the provider, `opened` once they are
  // through, `done` once they have answered either way. Local state by design:
  // the part on the wire never changes, so the card's own progress is the only
  // record of it — and a reload starting over is correct, since a tool that is
  // still unauthorized asks again anyway.
  const [stage, setStage] = useState<'idle' | 'opened' | 'done'>('idle');

  // Answering is the middle of the story, not the end: the receipt rides the
  // turn the answer starts, so the card itself leaves nothing behind.
  if (stage === 'done') return null;

  // Both buttons answer the SAME parked interrupt, and both answer it
  // explicitly: the decision rides a DataPart the orchestrator's middleware acts
  // on directly — approved retries the blocked tool (so a still-missing
  // credential asks again, as a card), declined tells the agent to stop pushing
  // the link. The prose is what a client that never negotiated the extension
  // falls back to; the model reads it and decides.
  const answer = (decision: 'approved' | 'declined') => {
    const approved = decision === 'approved';
    const prompt = approved
      ? tool
        ? format(CONTINUE_PROMPT_TOOL, { tool })
        : CONTINUE_PROMPT
      : tool
        ? format(DECLINE_PROMPT_TOOL, { tool })
        : DECLINE_PROMPT;
    send?.(prompt, {
      displayText: receiptHeadline(strings, approved ? 'authorized' : 'skipped', subject),
      displayKind: 'receipt',
      displayOutcome: approved ? 'authorized' : 'skipped',
      authorization: { decision },
    });
    // The turn the send starts carries the receipt, so the card leaves nothing
    // behind. Nothing is lost by a misclick either: the decision reached the
    // agent, and asking again re-runs the tool and raises a fresh card.
    setStage('done');
  };

  return (
    <InterruptCard
      slot="nannos-auth-required"
      className={className}
      icon={<ShieldCheckIcon aria-hidden="true" className="size-3.5 shrink-0" />}
      title={
        service ? format(strings['auth.titleService'], { service }) : strings['auth.title']
      }
    >
      <InterruptSection slot="nannos-auth-body">
        <span className="text-xs">
          {stage === 'opened'
            ? strings['auth.retryHint']
            : tool
              ? format(strings['auth.bodyTool'], { tool })
              : strings['auth.body']}
        </span>
        {data.authUrl ? (
          stage === 'idle' || !send ? (
            <InterruptActions>
              <Button asChild variant="outline" size="sm" className={ACTION_CLASS} data-slot="nannos-auth-action">
                {/* The anchor keeps its native behaviour (new window, middle-click,
                    copy-link); the click only moves the card forward. */}
                <a
                  href={data.authUrl}
                  target="_blank"
                  rel="noreferrer noopener"
                  onClick={() => setStage('opened')}
                >
                  {strings['auth.action']}
                  <ExternalLinkIcon aria-hidden="true" />
                </a>
              </Button>
              {send && (
                <Button
                  variant="ghost"
                  size="sm"
                  className={ACTION_CLASS}
                  data-slot="nannos-auth-skip"
                  onClick={() => answer('declined')}
                >
                  {strings['auth.skip']}
                </Button>
              )}
            </InterruptActions>
          ) : (
            <InterruptActions className="flex-wrap">
              <Button
                variant="outline"
                size="sm"
                className={ACTION_CLASS}
                data-slot="nannos-auth-done"
                onClick={() => answer('approved')}
              >
                <CheckIcon aria-hidden="true" />
                {strings['auth.doneAction']}
              </Button>
              <Button asChild variant="ghost" size="sm" className={ACTION_CLASS} data-slot="nannos-auth-retry">
                <a href={data.authUrl} target="_blank" rel="noreferrer noopener">
                  {strings['auth.retryAction']}
                  <ExternalLinkIcon aria-hidden="true" />
                </a>
              </Button>
            </InterruptActions>
          )
        ) : (
          // No URL anywhere: fall back to the raw wire text so the user at least
          // sees what the gateway asked for.
          data.message && <span className="whitespace-pre-wrap text-xs">{data.message}</span>
        )}
        {devMode && data.authUrl && data.message && (
          <div
            className={cn(
              'rounded-md border border-amber-500/50 bg-amber-500/5 px-2 py-1',
              'text-amber-600 text-xs dark:text-amber-500',
            )}
          >
            <p className="font-bold">[DEV MODE] RAW PART FROM GATEWAY:</p>
            <span className="whitespace-pre-wrap text-amber-600 text-xs dark:text-amber-500">
              {data.message}
            </span>
          </div>
        )}
      </InterruptSection>
    </InterruptCard>
  );
}
