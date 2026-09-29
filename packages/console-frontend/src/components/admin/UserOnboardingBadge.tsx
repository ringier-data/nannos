import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { UserOnboarding } from '@/api/generated';

interface UserOnboardingBadgeProps {
  /** Null for a service account, which never signs in interactively. */
  onboarding: UserOnboarding | null;
}

/**
 * What still stands between a user and running scheduled jobs that reach them. Renders
 * nothing once they can run them and every job reaches them, so a healthy list stays
 * quiet. Not-yet-signed-in is the normal state right after SCIM provisioning, so it reads
 * as pending, never as an error.
 *
 * Delivery is the third signal (#192): subscriptions on a chat channel the user never
 * signed in from. A channel Nannos can't judge (an older sign-in) isn't counted, so no
 * badge still doesn't prove every notification lands.
 */
export function UserOnboardingBadge({ onboarding }: UserOnboardingBadgeProps) {
  if (!onboarding) {
    return null;
  }
  const unreachable = onboarding.unreachable_subscriptions ?? 0;
  const unreachableHint =
    unreachable > 0
      ? `${unreachable === 1 ? 'One of their scheduled jobs delivers' : `${unreachable} of their scheduled jobs deliver`} to a chat channel Nannos can't reach them on, so its results don't arrive. Messaging Nannos there once, or moving the job to another channel, fixes it.`
      : null;
  if (onboarding.scheduler_ready && !unreachableHint) {
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
      : !onboarding.scheduler_ready
        ? {
            label: 'Scheduled jobs not ready',
            hint: "Has used Nannos, but hasn't signed in through the console or a client using the sign-in broker, so no scheduled job can run under their account yet.",
          }
        : {
            label: unreachable === 1 ? "Can't be reached on 1 job" : `Can't be reached on ${unreachable} jobs`,
            hint: unreachableHint,
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
        {/* Behind a sign-in problem, the delivery one is the next thing they'll hit. */}
        {!onboarding.scheduler_ready && unreachableHint && <p className="opacity-80">{unreachableHint}</p>}
      </TooltipContent>
    </Tooltip>
  );
}
