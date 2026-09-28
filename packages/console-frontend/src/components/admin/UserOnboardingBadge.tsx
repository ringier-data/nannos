import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { UserOnboarding } from '@/api/generated';

interface UserOnboardingBadgeProps {
  /** Null for a service account, which never signs in interactively. */
  onboarding: UserOnboarding | null;
}

/**
 * What still stands between a user and running scheduled jobs. Renders nothing once they
 * can run them, so a healthy list stays quiet. Not-yet-signed-in is the normal state
 * right after SCIM provisioning, so it reads as pending, never as an error.
 *
 * Deliberately says nothing about delivery: whether a notification reaches the user on a
 * chat channel is not known yet (#192), so no badge does not mean "reachable".
 */
export function UserOnboardingBadge({ onboarding }: UserOnboardingBadgeProps) {
  if (!onboarding || onboarding.scheduler_ready) {
    return null;
  }
  const { label, hint } = !onboarding.signed_in
    ? {
        label: 'Not signed in yet',
        hint: "Provisioned, but hasn't signed in to Nannos yet. Scheduled jobs they're subscribed to wait switched off until their first sign-in.",
      }
    : onboarding.sign_in_expired
      ? {
          label: 'Sign-in expired',
          hint: 'Their sign-in to Nannos expired (unused for 30 days, or revoked), so their scheduled jobs wait switched off until they sign in again.',
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
      <TooltipContent className="max-w-xs space-y-1">
        <p>{hint}</p>
        <p className="opacity-80">Whether notifications reach them on a chat channel isn't shown here yet.</p>
      </TooltipContent>
    </Tooltip>
  );
}
