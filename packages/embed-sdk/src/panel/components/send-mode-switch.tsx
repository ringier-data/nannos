/**
 * `<SendModeSwitch>` — what Enter does while the agent is busy: steer the
 * running answer, or stop it and start over. Sits directly left of send and
 * appears ONLY while a turn runs — with nothing running the two modes are the
 * same plain send, and a choice with no consequence is noise.
 *
 * Same hand-rolled menu as `<ApplyModeSwitch>`, for the same reason: Radix
 * popovers dismiss themselves inside a Shadow DOM (see that file). Positioned
 * against the composer's action row, hit-tested with `composedPath()`.
 */
import { useEffect, useRef, useState } from 'react';
import { CheckIcon, MessageSquareReplyIcon, OctagonXIcon, type LucideIcon } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { useStrings } from '../../react';
import { useSendMode, type SendMode } from '../send-mode';
import type { NannosStrings } from '../../i18n/keys';

interface ModeOption {
  mode: SendMode;
  icon: LucideIcon;
  title: keyof NannosStrings;
  description: keyof NannosStrings;
}

/** Gentle first: the list reads as "add to it" → "replace it". */
const OPTIONS: readonly ModeOption[] = [
  {
    mode: 'steer',
    icon: MessageSquareReplyIcon,
    title: 'sendMode.steer',
    description: 'sendMode.steerHint',
  },
  {
    mode: 'stop-and-send',
    icon: OctagonXIcon,
    title: 'sendMode.stopAndSend',
    description: 'sendMode.stopAndSendHint',
  },
];

export interface SendModeSwitchProps {
  className?: string;
}

export function SendModeSwitch({ className }: SendModeSwitchProps) {
  const strings = useStrings();
  const { mode, setMode } = useSendMode();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: Event) => {
      const path = event.composedPath();
      if (triggerRef.current && path.includes(triggerRef.current)) return;
      if (menuRef.current && path.includes(menuRef.current)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    window.addEventListener('pointerdown', onPointerDown, true);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('pointerdown', onPointerDown, true);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const active = OPTIONS.find((o) => o.mode === mode) ?? OPTIONS[0];
  const ActiveIcon = active.icon;

  return (
    <>
      <Button
        ref={triggerRef}
        data-slot="nannos-send-mode"
        data-mode={mode}
        type="button"
        variant="ghost"
        size="sm"
        className={cn('h-7 shrink-0 gap-1 px-1.5 text-muted-foreground text-xs', className)}
        aria-label={strings['sendMode.label']}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        <ActiveIcon aria-hidden="true" className="size-3.5 shrink-0" />
        <span className="truncate">{strings[active.title]}</span>
      </Button>

      {open && (
        <div
          ref={menuRef}
          data-slot="nannos-send-mode-menu"
          data-side="top"
          role="menu"
          aria-label={strings['sendMode.heading']}
          className="absolute right-1 bottom-full z-30 mb-2 flex w-80 max-w-[calc(100%-0.5rem)] flex-col gap-0.5 rounded-lg border bg-popover p-1 text-popover-foreground shadow-lg"
        >
          <p className="px-2 py-1.5 font-medium text-muted-foreground text-xs">
            {strings['sendMode.heading']}
          </p>
          {OPTIONS.map((option) => {
            const Icon = option.icon;
            const selected = option.mode === mode;
            return (
              <button
                key={option.mode}
                data-slot="nannos-send-mode-option"
                data-mode={option.mode}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                className={cn(
                  'flex w-full cursor-pointer items-start gap-2.5 rounded-md px-2 py-2 text-left',
                  'hover:bg-accent hover:text-accent-foreground',
                  selected && 'bg-accent text-accent-foreground',
                )}
                onClick={() => {
                  setMode(option.mode);
                  setOpen(false);
                }}
              >
                <Icon aria-hidden="true" className="size-4 shrink-0 self-center" />
                <span className="min-w-0 flex-1">
                  <span className={cn('block text-sm', selected ? 'font-bold' : 'font-medium')}>
                    {strings[option.title]}
                  </span>
                  <span className="block text-muted-foreground text-xs">
                    {strings[option.description]}
                  </span>
                </span>
                {selected && <CheckIcon aria-hidden="true" className="size-4 shrink-0 self-center" />}
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}
