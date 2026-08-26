/**
 * The secondary-authorization prompt: shown when a tool the assistant wanted to
 * use needs the end-user's consent (the MCP gateway's `need-credentials`).
 *
 * The card walks the user through the whole detour in place: "Authorize" opens
 * the provider in a new window, and once it is open the card switches to "Done,
 * continue" — the click that tells the agent to try again — with a ghost
 * "Re-authorize" beside it for the attempt that did not take. Confirming hides
 * the card: the turn it sends is the next thing in the thread.
 *
 * The copy is OURS and localized. The wire message that comes with the
 * `auth-required` status is the gateway addressing the agent ("You must tell
 * the end-user to…") — never end-user copy — so it is rendered only when we
 * could not find a URL to link (better stranded text than no way forward), and
 * in dev mode for debugging. The turn the confirm button sends is agent-facing
 * too, so it stays English and out of the localized strings; the user sees the
 * localized chip label instead.
 */
import { useState } from 'react';
import { CheckIcon, ExternalLinkIcon, ShieldCheckIcon } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '../../components/ui/alert';
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { format, useStrings } from '../../react';
import { useDevMode } from '../dev-mode';
import type { UseNannosChatValue } from '../hooks/use-nannos-chat';

export interface AuthRequiredCardProps {
  data: { authUrl?: string; tool?: string; message?: string };
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
const ACTION_CLASS = 'h-7 gap-1 px-2 text-xs has-[>svg]:px-2 [&_svg]:size-3';

/** What "Done, continue" actually sends: agent-facing, so English. */
const CONTINUE_PROMPT = 'I have completed the authorization. Please retry what needed it and continue.';
const CONTINUE_PROMPT_TOOL =
  'I have completed the authorization for {tool}. Please retry what needed it and continue.';

export function AuthRequiredCard({ data, send, className }: AuthRequiredCardProps) {
  const strings = useStrings();
  const devMode = useDevMode();
  const tool = data.tool && !OPAQUE_TOOLS.has(data.tool) ? data.tool : undefined;

  // `idle` until the user has been sent to the provider, `done` once they say
  // they are through. Local state by design: the part on the wire never
  // changes, so the card's own progress is the only record of it — and a
  // reload starting over is correct, since a tool that is still unauthorized
  // asks again anyway.
  const [stage, setStage] = useState<'idle' | 'opened' | 'done'>('idle');
  if (stage === 'done') return null;

  const confirm = () => {
    send?.(tool ? format(CONTINUE_PROMPT_TOOL, { tool }) : CONTINUE_PROMPT, {
      displayText: strings['auth.doneChip'],
    });
    setStage('done');
  };

  return (
    <Alert
      data-slot="nannos-auth-required"
      className={cn(
        // Tighter than the stock alert, and a smaller leading icon — the column
        // it sits in has to shrink with it, hence the grid override.
        'gap-y-1 px-2.5 py-2 text-xs has-[>svg]:grid-cols-[calc(var(--spacing)*3.5)_1fr] has-[>svg]:gap-x-2 [&>svg]:size-3.5',
        className,
      )}
    >
      <ShieldCheckIcon aria-hidden="true" />
      <AlertTitle className="text-xs">{strings['auth.title']}</AlertTitle>
      <AlertDescription className="flex flex-col items-start gap-1.5 break-words text-xs">
        <span>{tool ? format(strings['auth.bodyTool'], { tool }) : strings['auth.body']}</span>
        {data.authUrl ? (
          stage === 'idle' || !send ? (
            <Button asChild size="sm" className={ACTION_CLASS} data-slot="nannos-auth-action">
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
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-1.5">
                <Button
                  size="sm"
                  className={ACTION_CLASS}
                  data-slot="nannos-auth-done"
                  onClick={confirm}
                >
                  <CheckIcon aria-hidden="true" />
                  {strings['auth.doneAction']}
                </Button>
                <Button
                  asChild
                  variant="ghost"
                  size="sm"
                  className={ACTION_CLASS}
                  data-slot="nannos-auth-retry"
                >
                  <a href={data.authUrl} target="_blank" rel="noreferrer noopener">
                    {strings['auth.retryAction']}
                    <ExternalLinkIcon aria-hidden="true" />
                  </a>
                </Button>
              </div>
              <span className="text-muted-foreground">{strings['auth.retryHint']}</span>
            </>
          )
        ) : (
          // No URL anywhere: fall back to the raw wire text so the user at least
          // sees what the gateway asked for.
          data.message && <span className="whitespace-pre-wrap">{data.message}</span>
        )}
        {devMode && data.authUrl && data.message && (
          <div className="border border-amber-500/50 rounded-md bg-amber-500/5 px-2 py-1 text-xs text-amber-600 dark:text-amber-500">
            <p className="font-bold">[DEV MODE] RAW PART FROM GATEWAY:</p>
          <span className="whitespace-pre-wrap text-amber-600 text-xs dark:text-amber-500">
            {data.message}
          </span>
          </div>
        )}
      </AlertDescription>
    </Alert>
  );
}
