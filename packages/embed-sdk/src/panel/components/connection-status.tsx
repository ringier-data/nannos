/**
 * Small connection-status chip for the panel footer. `unauthenticated` renders
 * a sign-in button instead of a dead label — `useAssistant().open()` runs the
 * gesture-safe login flow (popup-legal) internally.
 */
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { useAssistant, useStrings } from '../../react';
import type { NannosStatus } from '../../core';
import type { NannosStrings } from '../../i18n/keys';

const STATUS_LABEL_KEY = {
  connected: 'status.connected',
  connecting: 'status.connecting',
  disconnected: 'status.disconnected',
  unauthenticated: 'status.unauthenticated',
  authError: 'status.error',
} as const satisfies Record<NannosStatus, keyof NannosStrings>;

const STATUS_DOT_CLASS: Record<NannosStatus, string> = {
  connected: 'bg-emerald-500',
  connecting: 'bg-amber-500 animate-pulse',
  disconnected: 'bg-muted-foreground',
  unauthenticated: 'bg-amber-500',
  authError: 'bg-destructive',
};

export interface ConnectionStatusProps {
  className?: string;
}

export function ConnectionStatus({ className }: ConnectionStatusProps) {
  const assistant = useAssistant();
  const strings = useStrings();
  const status = assistant.status;

  return (
    <div
      data-slot="nannos-connection-status"
      className={cn(
        'flex items-center gap-2 px-3 py-1.5 text-muted-foreground text-xs',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn('size-1.5 shrink-0 rounded-full', STATUS_DOT_CLASS[status])}
      />
      <span className="truncate">{strings[STATUS_LABEL_KEY[status]]}</span>
      {status === 'unauthenticated' && (
        <Button
          data-slot="nannos-sign-in"
          type="button"
          variant="outline"
          size="sm"
          className="ml-auto h-6 px-2 text-xs"
          onClick={() => assistant.open()}
        >
          {strings['status.signIn']}
        </Button>
      )}
    </div>
  );
}
