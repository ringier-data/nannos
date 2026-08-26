/**
 * `<ApplyModeSwitch>` — the composer's apply-mode control, next to send.
 *
 * It sits where the user is when they ask for a fill, so the answer to "will it
 * ask me first?" is in view at the moment the question matters. The trigger
 * shows the mode that is ON; clicking opens a menu ABOVE it listing both modes
 * with what each one actually does, and a check on the current one — the label
 * alone ("Manual") cannot carry that, and a mode nobody understands is a mode
 * nobody changes.
 *
 * Hand-rolled rather than Radix `Popover`, for the same reason the conversation
 * history overlay is: the panel lives in a Shadow DOM, and there the Radix
 * popover does not open at all. Established by swapping the two back and forth
 * against a live panel — Radix: nothing; this: works; Radix again: nothing.
 * The mechanism fits Radix's dismissable layer, which listens on `document`,
 * where every event from inside a shadow root is RETARGETED to the shadow host:
 * its outside-click test compares against an element it can never match, so the
 * layer dismisses itself as it opens. Nothing renders and nothing is logged,
 * which is why this reads as a dead button rather than a broken one.
 *
 * The hit test here is `composedPath()`, the one shadow-aware answer to "did
 * this event come from inside my subtree", and there is no portal to escape
 * through: the menu is positioned against the composer's action row
 * (`relative`), which also bounds its width to the panel.
 *
 * Renders nothing when the host fixed the mode via the panel's `applyMode`
 * prop — a locked setting must not look adjustable.
 */
import { useEffect, useRef, useState } from 'react';
import { CheckIcon, HandIcon, PencilIcon, type LucideIcon } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { useStrings } from '../../react';
import { useApplyModeControls, type ApplyMode } from '../apply-mode';
import type { NannosStrings } from '../../i18n/keys';

interface ModeOption {
  mode: ApplyMode;
  icon: LucideIcon;
  title: keyof NannosStrings;
  description: keyof NannosStrings;
}

/** Cautious first: the list reads as "ask me" → "go ahead". */
const OPTIONS: readonly ModeOption[] = [
  {
    mode: 'manual',
    icon: HandIcon,
    title: 'applyMode.manual',
    description: 'applyMode.manualHint',
  },
  {
    mode: 'allow-edits',
    icon: PencilIcon,
    title: 'applyMode.allowEdits',
    description: 'applyMode.allowEditsHint',
  },
];

export interface ApplyModeSwitchProps {
  className?: string;
}

export function ApplyModeSwitch({ className }: ApplyModeSwitchProps) {
  const strings = useStrings();
  const { mode, locked, setMode } = useApplyModeControls();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Escape, or a pointer down anywhere else, closes it. Bound on the window:
  // events reach it from inside a shadow root, and `composedPath()` is the only
  // hit test that still works there (`event.target` is the shadow HOST).
  // Capture phase, so this runs before the trigger's own toggle — a second
  // click on the trigger then closes the menu exactly once, not twice.
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

  if (locked) return null;

  const active = OPTIONS.find((o) => o.mode === mode) ?? OPTIONS[0];
  const ActiveIcon = active.icon;

  return (
    <>
      <Button
        ref={triggerRef}
        data-slot="nannos-apply-mode"
        data-mode={mode}
        type="button"
        variant="ghost"
        size="sm"
        className={cn('h-7 shrink-0 gap-1 px-1.5 text-muted-foreground text-xs', className)}
        aria-label={strings['applyMode.label']}
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
          data-slot="nannos-apply-mode-menu"
          data-side="top"
          role="menu"
          aria-label={strings['applyMode.heading']}
          // Above the row, right-aligned with it, and never wider than the
          // panel: a narrow docked panel shrinks the menu instead of pushing it
          // out over the host page.
          // `gap-0.5` (2px) separates the rows: they carry their own hover and
          // selected backgrounds, which read as one block when they touch.
          className="absolute right-1 bottom-full z-30 mb-2 flex w-80 max-w-[calc(100%-0.5rem)] flex-col gap-0.5 rounded-lg border bg-popover p-1 text-popover-foreground shadow-lg"
        >
          <p className="px-2 py-1.5 font-medium text-muted-foreground text-xs">
            {strings['applyMode.heading']}
          </p>
          {OPTIONS.map((option) => {
            const Icon = option.icon;
            const selected = option.mode === mode;
            return (
              <button
                key={option.mode}
                data-slot="nannos-apply-mode-option"
                data-mode={option.mode}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                className={cn(
                  // The row is top-aligned so the two-line text block starts at
                  // the top; the icons opt out with `self-center`, which centres
                  // them against the whole block rather than its first line.
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
                  {/* The mode that is ON reads bold — the check is at the far
                      right, so the name itself has to carry it too. */}
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
