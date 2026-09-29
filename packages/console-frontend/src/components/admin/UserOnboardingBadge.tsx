import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { UserOnboarding } from '@/api/generated';
import { issueHint, issueLabel, severityClassName } from './onboarding';

interface UserOnboardingBadgeProps {
  /** Null for a service account, which never signs in interactively. */
  onboarding: UserOnboarding | null;
  /**
   * Also badge a user whose only issues are ones Nannos can't judge. Off by default so a
   * healthy list stays quiet; on while the list is filtered by an issue, so a row that
   * matched says why.
   */
  showInfo?: boolean;
}

/**
 * The worst thing standing between a user and scheduled jobs that run and reach them,
 * plus how many more there are (#311). Renders nothing when nothing needs attention, so
 * a healthy list stays quiet: an issue Nannos can't judge (`info`) never raises a badge
 * on its own, but is listed in the tooltip behind one that does.
 *
 * What the issues cover is the listing's scope: every job on the Users page, only the
 * group's default jobs on a group's member list.
 */
export function UserOnboardingBadge({ onboarding, showInfo = false }: UserOnboardingBadgeProps) {
  if (!onboarding || (!onboarding.severity && !showInfo)) {
    return null;
  }
  const [worst, ...rest] = onboarding.issues;
  if (!worst) {
    return null;
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className={severityClassName(worst.severity)}>
          {issueLabel(worst)}
          {rest.length > 0 && <span className="ml-1 opacity-70">+{rest.length}</span>}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-sm space-y-1.5">
        <p>{issueHint(worst)}</p>
        {rest.map((issue, i) => (
          <p key={i} className="opacity-80">
            {issueLabel(issue)}
            {issue.workspace_id ? ` · workspace ${issue.workspace_id}` : ''}
            {issue.jobs.length > 0 ? ` · ${issue.jobs.length} job${issue.jobs.length === 1 ? '' : 's'}` : ''}
          </p>
        ))}
      </TooltipContent>
    </Tooltip>
  );
}
