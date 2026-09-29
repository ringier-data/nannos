import type { IssueSeverity, OnboardingIssue, OnboardingIssueKind, OnboardingSummary, UserSort } from '@/api/generated';

/**
 * The words for an onboarding issue (#311), shared by the badge, the user detail card and
 * the filters so an administrator reads the same thing in every place.
 */

function jobsPhrase(n: number): string {
  return n === 1 ? 'one scheduled job' : `${n} scheduled jobs`;
}

/** Where a delivery issue happens: the client, then its workspace when known. */
export function issueWhere(issue: OnboardingIssue): string | null {
  if (!issue.client_id) {
    return null;
  }
  const client = issue.client_name ?? issue.client_id;
  return issue.workspace_id ? `${client} · workspace ${issue.workspace_id}` : client;
}

/** A short label: what is missing, and where. */
export function issueLabel(issue: OnboardingIssue): string {
  const where = issue.client_name ?? issue.client_id ?? '';
  switch (issue.kind) {
    case 'not_signed_in':
      return 'Not signed in yet';
    case 'sign_in_expired':
      return 'Sign-in expired';
    case 'scheduler_not_ready':
      return 'Scheduled jobs not ready';
    // A hold whose channel was since deleted has no client to name.
    case 'unreachable':
      return where ? `Can't be reached on ${where}` : "Can't be reached: channel removed";
    case 'undelivered':
      return where ? `Not delivered on ${where}` : 'Not delivered: channel removed';
    case 'agent_inaccessible':
      return issue.agent_name ? `No access to ${issue.agent_name}` : 'No access to an agent';
    case 'access_revoked':
      return 'Job access revoked';
    case 'needs_resume':
      return 'Switched off, needs resuming';
    case 'unknown_reachability':
      return `Delivery unverified on ${where}`;
  }
}

/** One sentence: what it costs, and what fixes it. */
export function issueHint(issue: OnboardingIssue): string {
  const n = issue.jobs.length;
  const where = issueWhere(issue) ?? 'that client';
  switch (issue.kind) {
    case 'not_signed_in':
      return n > 0
        ? `Provisioned, but hasn't signed in to Nannos yet, so ${jobsPhrase(n)} wait switched off until their first sign-in.`
        : "Provisioned, but hasn't signed in to Nannos yet. Nothing waits on it so far.";
    case 'sign_in_expired':
      return n > 0
        ? `Their sign-in expired (unused for 30 days, or revoked), so ${jobsPhrase(n)} wait switched off. Signing in to Nannos again fixes it.`
        : 'Their sign-in expired (unused for 30 days, or revoked). The next scheduled job they get will wait until they sign in again.';
    case 'scheduler_not_ready':
      return n > 0
        ? `Has used Nannos, but never signed in through the console or a client using the sign-in broker, so ${jobsPhrase(n)} can't run under their account. Signing in to the console once fixes it.`
        : 'Has used Nannos, but never signed in through the console or a client using the sign-in broker, so no scheduled job can run under their account yet.';
    case 'unreachable':
      if (!issue.client_id) {
        return `The delivery channel of ${jobsPhrase(n)} was removed, so they stay switched off. Moving the job to another channel fixes it.`;
      }
      return `${n === 1 ? 'One scheduled job delivers' : `${n} scheduled jobs deliver`} to ${where}, where they haven't signed in to Nannos, so the results don't arrive. Messaging Nannos there once, or moving the job to another channel, fixes it.`;
    case 'undelivered':
      if (!issue.client_id) {
        return `The delivery channel of ${jobsPhrase(n)} was removed, so they stay switched off. Moving the job to another channel fixes it.`;
      }
      return `${where} found no one to deliver ${jobsPhrase(n)} to, so they were switched off. Messaging Nannos there once, then switching the job back on, fixes it.`;
    case 'agent_inaccessible':
      return `They lost access to ${issue.agent_name ?? 'the agent'}, so ${jobsPhrase(n)} running it wait switched off. Sharing the agent with one of their groups again, then resuming the job, fixes it.`;
    case 'needs_resume':
      return `${jobsPhrase(n)[0].toUpperCase()}${jobsPhrase(n).slice(1)} ${n === 1 ? 'was' : 'were'} switched off by an error Nannos doesn't clear on its own (a failed token refresh, or a hold that outlived their sign-in). Resuming the job switches it back on.`;
    case 'access_revoked':
      return `The share behind ${jobsPhrase(n)} was withdrawn, so they wait switched off. Sharing the job with them again restores it.`;
    case 'unknown_reachability':
      return `Nannos can't tell whether ${jobsPhrase(n)} on ${where} reach them: they signed in there the old way, or the channel's workspace isn't known. Nothing is switched off.`;
  }
}

/** The badge / chip look per severity. */
export function severityClassName(severity: IssueSeverity): string {
  switch (severity) {
    case 'blocking':
      return 'border-destructive/40 text-destructive';
    case 'pending':
      return 'text-muted-foreground';
    case 'info':
      return 'text-muted-foreground border-dashed';
  }
}

export const SEVERITY_LABEL: Record<IssueSeverity, string> = {
  blocking: 'Blocking',
  pending: 'Pending',
  info: "Can't tell",
};

const KIND_LABEL: Record<OnboardingIssueKind, string> = {
  not_signed_in: 'Not signed in yet',
  sign_in_expired: 'Sign-in expired',
  scheduler_not_ready: 'Scheduled jobs not ready',
  unreachable: "Can't be reached",
  undelivered: 'Not delivered',
  agent_inaccessible: 'No access to an agent',
  access_revoked: 'Job access revoked',
  needs_resume: 'Switched off, needs resuming',
  unknown_reachability: 'Delivery unverified',
};

/** What the attention filter can be. */
export type AttentionFilter = 'all' | 'attention' | 'blocking' | 'pending';

/**
 * The onboarding filters as the page holds them. `issue` is `all`, a kind, or
 * `kind|client_id` for a delivery issue on one client, matching one summary entry.
 */
export interface OnboardingFilterValue {
  attention: AttentionFilter;
  issue: string;
}

export const NO_ONBOARDING_FILTER: OnboardingFilterValue = { attention: 'all', issue: 'all' };

export function isOnboardingFiltered(value: OnboardingFilterValue): boolean {
  return value.attention !== 'all' || value.issue !== 'all';
}

/** The list endpoints' query for a filter value. Any filter sorts worst first. */
export function onboardingQuery(value: OnboardingFilterValue): {
  severity?: IssueSeverity[];
  issue?: OnboardingIssueKind[];
  client_id?: string;
  sort?: UserSort;
} {
  const severity: IssueSeverity[] | undefined =
    value.attention === 'attention'
      ? ['blocking', 'pending']
      : value.attention === 'all'
        ? undefined
        : [value.attention];
  // Split on the first '|' only: a client id may itself contain one.
  const separator = value.issue.indexOf('|');
  const [kind, clientId] =
    value.issue === 'all'
      ? [undefined, undefined]
      : separator < 0
        ? [value.issue, undefined]
        : [value.issue.slice(0, separator), value.issue.slice(separator + 1)];
  return {
    severity,
    issue: kind ? [kind as OnboardingIssueKind] : undefined,
    client_id: clientId || undefined,
    sort: isOnboardingFiltered(value) ? 'severity' : undefined,
  };
}

/** The issue filter's options, one per summary entry, most users first. */
export function issueOptions(summary: OnboardingSummary | undefined): { value: string; label: string }[] {
  return (summary?.issues ?? []).map((entry) => {
    const where = entry.client_id ? ` on ${entry.client_name ?? entry.client_id}` : '';
    return {
      value: entry.client_id ? `${entry.kind}|${entry.client_id}` : entry.kind,
      label: `${KIND_LABEL[entry.kind]}${where} (${entry.users})`,
    };
  });
}
