/**
 * Apply mode — how much the assistant may do to a form on its own.
 *
 * Only ONE thing is gated: a `client_action` directive of kind `apply`, the
 * write into a host-registered form. Everything else the assistant does is
 * unaffected, and the assistant is never told which mode is on — the panel
 * answers its approval request either way, so a fill behaves identically from
 * the agent's side. Nothing is persisted to a backend in either mode: the form
 * is still the user's to review and save.
 *
 *  - `manual` (default) — every apply raises the approval card, listing the
 *    values, and waits for a click.
 *  - `allow-edits` — the panel approves it itself: the directive runs in the
 *    page and the turn resumes once, with no card and no click.
 *
 * The choice is a per-viewer convenience, so it lives in `localStorage` and
 * never leaves the browser. A host that wants to decide for its users passes
 * the `applyMode` prop, which wins and hides the header control (same shape as
 * `devMode`) — a locked mode must not look adjustable.
 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

export type ApplyMode = 'manual' | 'allow-edits';

const STORAGE_KEY = 'nannos:applyMode';

/** Manual unless the viewer chose otherwise — the assistant asking first is
 *  the safe default, and a stored value from another origin cannot reach here
 *  (each host origin has its own localStorage). */
export function readStoredApplyMode(): ApplyMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'allow-edits' ? 'allow-edits' : 'manual';
  } catch {
    // Private windows and blocked site data both throw on access.
    return 'manual';
  }
}

export interface ApplyModeValue {
  mode: ApplyMode;
  /** The host fixed the mode: the header offers no control. */
  locked: boolean;
  setMode: (mode: ApplyMode) => void;
}

const ApplyModeContext = createContext<ApplyModeValue>({
  mode: 'manual',
  locked: true,
  setMode: () => {},
});

/** The mode itself — what the apply path checks. Defaults to `manual` outside
 *  a provider, so a host surface that never mounted one keeps asking. */
export function useApplyMode(): ApplyMode {
  return useContext(ApplyModeContext).mode;
}

/** The full control surface, for the header switch. */
export function useApplyModeControls(): ApplyModeValue {
  return useContext(ApplyModeContext);
}

export function ApplyModeProvider({
  mode: fixed,
  children,
}: {
  /** Host override. Set → that mode, no control. Unset → the viewer's choice. */
  mode?: ApplyMode;
  children: ReactNode;
}) {
  const [stored, setStored] = useState<ApplyMode>(readStoredApplyMode);

  const setMode = useCallback((next: ApplyMode) => {
    setStored(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Not persisting is survivable; the mode still holds for this session.
    }
  }, []);

  const value = useMemo<ApplyModeValue>(
    () => ({ mode: fixed ?? stored, locked: fixed !== undefined, setMode }),
    [fixed, stored, setMode],
  );
  return <ApplyModeContext.Provider value={value}>{children}</ApplyModeContext.Provider>;
}
