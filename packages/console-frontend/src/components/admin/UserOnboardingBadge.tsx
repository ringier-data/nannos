import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { UserOnboarding } from '@/api/generated';

interface UserOnboardingBadgeProps {
  onboarding: UserOnboarding;
  /** Machine identities never sign in interactively, so onboarding says nothing about them. */
  isServiceAccount?: boolean;
}

/**
 * What still stands between a user and being usable. Renders nothing once they are fully
 * onboarded, so a healthy list stays quiet. Not-yet-signed-in is the normal state right
 * after SCIM provisioning, so it reads as pending, never as an error.
 */
export function UserOnboardingBadge({ onboarding, isServiceAccount }: UserOnboardingBadgeProps) {
  if (isServiceAccount || (onboarding.signed_in && onboarding.scheduler_ready)) {
    return null;
  }
  const { label, hint } = !onboarding.signed_in
    ? {
        label: 'Not signed in yet',
        hint: "Provisioned, but hasn't signed in to Nannos yet. Scheduled jobs they're subscribed to wait switched off until their first sign-in.",
      }
    : {
        label: 'Scheduled jobs not ready',
        hint: "Has used Nannos, but hasn't signed in through the console or a client using the sign-in broker, so no scheduled job can run under their account yet.",
      };

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className="text-muted-foreground">
          {label}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{hint}</TooltipContent>
    </Tooltip>
  );
}
