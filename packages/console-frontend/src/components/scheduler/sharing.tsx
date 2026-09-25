/**
 * The little that a shared scheduled job adds to the scheduler pages (ADR-0010).
 *
 * Renders nothing at all for a job nobody else runs — which is every job until somebody
 * shares one. See `@/lib/sharedJobs` for how the flat job view is read.
 */
import { Lock, User, Users } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { ScheduledJob } from '@/api/generated/types.gen';
import { isOwnJob, subscriberCount } from '@/lib/sharedJobs';

/** Who else this job belongs to — nothing for a job that is only the viewer's own. */
export function SharingBadge({ job }: { job: ScheduledJob }) {
  if (!isOwnJob(job)) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant="outline" className="gap-1">
            <Users className="h-3 w-3" /> Shared
          </Badge>
        </TooltipTrigger>
        <TooltipContent>
          Shared with you by {job.owner_email ?? 'another user'}
          {job.activated_by === 'group' && ' \u2014 activated by a group default'}. It runs under
          your account.
        </TooltipContent>
      </Tooltip>
    );
  }
  const subscribers = subscriberCount(job);
  if (subscribers > 1) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant="outline" className="gap-1">
            <Users className="h-3 w-3" /> {subscribers}
          </Badge>
        </TooltipTrigger>
        <TooltipContent>
          You and {subscribers - 1} other{subscribers > 2 ? 's' : ''} run this job, each under
          their own account.
        </TooltipContent>
      </Tooltip>
    );
  }
  return null;
}

/**
 * Whose a part of the job page is, on a job somebody else also runs (ADR-0010).
 *
 * A shared job is one definition plus a subscription per person, and the page shows
 * both folded together. Without a marker per section there was no telling which field
 * changes everyone's job and which only your own.
 */
export function OwnershipBadge({ kind, children }: { kind: 'shared' | 'mine' | 'locked'; children: string }) {
  const Icon = kind === 'shared' ? Users : kind === 'mine' ? User : Lock;
  return (
    <Badge
      variant="outline"
      className={
        kind === 'mine'
          ? 'gap-1 border-sky-500/50 text-sky-700 dark:text-sky-400'
          : kind === 'shared'
            ? 'gap-1 border-violet-500/50 text-violet-700 dark:text-violet-400'
            : 'gap-1 text-muted-foreground'
      }
    >
      <Icon className="h-3 w-3" /> {children}
    </Badge>
  );
}
