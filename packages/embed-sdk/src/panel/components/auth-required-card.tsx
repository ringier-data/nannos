/**
 * The secondary-authorization prompt: shown when a tool the assistant wanted to
 * use needs the end-user's consent (the MCP gateway's `need-credentials`).
 *
 * The copy is OURS and localized. The wire message that comes with the
 * `auth-required` status is the gateway addressing the agent ("You must tell
 * the end-user to…") — never end-user copy — so it is rendered only when we
 * could not find a URL to link (better stranded text than no way forward), and
 * in dev mode for debugging.
 */
import { ExternalLinkIcon, ShieldCheckIcon } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '../../components/ui/alert';
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { format, useStrings } from '../../react';
import { useDevMode } from '../dev-mode';

export interface AuthRequiredCardProps {
  data: { authUrl?: string; tool?: string; message?: string };
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

export function AuthRequiredCard({ data, className }: AuthRequiredCardProps) {
  const strings = useStrings();
  const devMode = useDevMode();
  const tool = data.tool && !OPAQUE_TOOLS.has(data.tool) ? data.tool : undefined;

  return (
    <Alert data-slot="nannos-auth-required" className={cn('gap-y-2', className)}>
      <ShieldCheckIcon aria-hidden="true" />
      <AlertTitle>{strings['auth.title']}</AlertTitle>
      <AlertDescription className="flex flex-col items-start gap-2 break-words">
        <span>{tool ? format(strings['auth.bodyTool'], { tool }) : strings['auth.body']}</span>
        {data.authUrl ? (
          <>
            <Button asChild size="sm" data-slot="nannos-auth-action">
              <a href={data.authUrl} target="_blank" rel="noreferrer noopener">
                {strings['auth.action']}
                <ExternalLinkIcon aria-hidden="true" />
              </a>
            </Button>
            <span className="text-muted-foreground text-xs">{strings['auth.retryHint']}</span>
          </>
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
