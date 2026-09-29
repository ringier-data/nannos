import { SelectItem } from '@/components/ui/select';
import type { DeliveryChannel, DeliveryReachability } from '@/api/scheduler';

/**
 * The channel picker's options. A channel the user can't receive on (they never signed
 * in to Nannos from there, #192) is greyed out with how to activate it, since saving it
 * would be refused. The job's saved channel stays selectable even then, as the backend
 * accepts it unchanged, so an edit that moved away from it can move back; so does the
 * one currently selected, so the picker always shows what it holds.
 */
export function DeliveryChannelOptions({
  channels,
  selected,
  saved,
}: {
  channels: DeliveryChannel[];
  selected?: string;
  saved?: string;
}) {
  return channels.map((ch) => {
    const id = String(ch.id);
    const unreachable = ch.reachability === 'unreachable' && id !== selected && id !== saved;
    return (
      <SelectItem key={ch.id} value={String(ch.id)} disabled={unreachable}>
        {ch.name}
        {unreachable ? (
          <span className="ml-2 text-xs text-muted-foreground">— message Nannos there once to activate it</span>
        ) : (
          ch.description && <span className="ml-2 text-xs text-muted-foreground">— {ch.description}</span>
        )}
      </SelectItem>
    );
  });
}

/**
 * Whether the selected channel reaches the user, under the picker. Quiet when it does.
 * 'unknown' only warns: an older sign-in can't be judged, and is never refused.
 */
export function DeliveryReachabilityNote({
  reachability,
  channelName,
}: {
  reachability?: DeliveryReachability | null;
  channelName?: string;
}) {
  if (reachability === 'unreachable') {
    return (
      <p className="text-xs text-destructive">
        Nannos can't reach you on {channelName ?? 'this channel'}, because you haven't signed in to Nannos from
        there. Message Nannos on it once to activate it.
      </p>
    );
  }
  if (reachability === 'unknown') {
    return (
      <p className="text-xs text-muted-foreground">
        Nannos can't confirm it reaches you on {channelName ?? 'this channel'}. If results don't arrive, message
        Nannos there once.
      </p>
    );
  }
  return null;
}
