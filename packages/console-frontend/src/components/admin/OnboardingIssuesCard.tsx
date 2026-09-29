import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import type { UserOnboarding } from '@/api/generated';
import { SEVERITY_LABEL, issueHint, issueLabel, severityClassName } from './onboarding';

interface OnboardingIssuesCardProps {
  onboarding: UserOnboarding;
}

/**
 * Every onboarding issue of one user, worst first, with the jobs it stops and what fixes
 * it (#311). Jobs are named, not linked: a job opens only for its subscribers.
 */
export function OnboardingIssuesCard({ onboarding }: OnboardingIssuesCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Onboarding</CardTitle>
        <CardDescription>What stands between them and scheduled jobs that run and reach them</CardDescription>
      </CardHeader>
      <CardContent>
        {onboarding.issues.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing is missing.</p>
        ) : (
          <ul className="space-y-4">
            {onboarding.issues.map((issue, i) => {
              return (
                <li key={i} className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className={severityClassName(issue.severity)}>
                      {SEVERITY_LABEL[issue.severity]}
                    </Badge>
                    <span className="font-medium">{issueLabel(issue)}</span>
                    {issue.workspace_id && (
                      <span className="text-sm text-muted-foreground">workspace {issue.workspace_id}</span>
                    )}
                  </div>
                  <p className="text-sm text-muted-foreground">{issueHint(issue)}</p>
                  {(issue.channel_names?.length ?? 0) > 0 && (
                    <p className="text-xs text-muted-foreground">Channel: {issue.channel_names?.join(', ')}</p>
                  )}
                  {issue.jobs.length > 0 && (
                    <p className="text-xs text-muted-foreground">
                      {issue.jobs.length === 1 ? 'Job' : 'Jobs'}: {issue.jobs.map((job) => job.name).join(', ')}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
