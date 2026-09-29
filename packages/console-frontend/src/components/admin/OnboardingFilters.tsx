import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import type { OnboardingSummary } from '@/api/generated';
import { issueOptions, type AttentionFilter, type OnboardingFilterValue } from './onboarding';

interface OnboardingFiltersProps {
  value: OnboardingFilterValue;
  onChange: (value: OnboardingFilterValue) => void;
  /** Counts for the options, over the same scope as the list. */
  summary: OnboardingSummary | undefined;
}

/**
 * Filter a people listing by what they are missing (#311): how bad (attention) and what
 * (issue, per client for delivery issues). Both filter on the server, so every match is
 * reachable whatever page is showing.
 */
export function OnboardingFilters({ value, onChange, summary }: OnboardingFiltersProps) {
  const attentionCount = summary ? summary.blocking + summary.pending : undefined;
  const count = (n: number | undefined) => (n === undefined ? '' : ` (${n})`);
  const options = issueOptions(summary);
  // Keep a selected issue selectable while a narrower search has no one left with it.
  const selectedMissing = value.issue !== 'all' && !options.some((o) => o.value === value.issue);

  return (
    <>
      <Select
        value={value.attention}
        onValueChange={(attention) => onChange({ ...value, attention: attention as AttentionFilter })}
      >
        <SelectTrigger className="w-[210px]" aria-label="Filter by onboarding severity">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Everyone</SelectItem>
          <SelectItem value="attention">Needs attention{count(attentionCount)}</SelectItem>
          <SelectItem value="blocking">Blocking{count(summary?.blocking)}</SelectItem>
          <SelectItem value="pending">Pending{count(summary?.pending)}</SelectItem>
        </SelectContent>
      </Select>
      <Select value={value.issue} onValueChange={(issue) => onChange({ ...value, issue })}>
        <SelectTrigger className="w-[260px]" aria-label="Filter by onboarding issue">
          <SelectValue placeholder="Any issue" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Any issue</SelectItem>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
          {selectedMissing && (
            <SelectItem value={value.issue} disabled>
              No one in this view
            </SelectItem>
          )}
        </SelectContent>
      </Select>
    </>
  );
}
