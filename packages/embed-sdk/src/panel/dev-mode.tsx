/**
 * Developer-mode flag, shared through context so the thread, header and
 * inspector can all react to it without prop drilling.
 *
 * Two levels: dev mode is AVAILABLE (the panel's `devMode` prop, or — without
 * a host rebuild — `localStorage['nannos:dev']='1'`), and while available it
 * can be flipped ACTIVE/inactive live from the panel header, so the developer
 * can preview the exact end-user view without leaving dev mode.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';

/** `devMode` prop wins when set; otherwise the localStorage escape hatch. */
export function resolveDevMode(prop?: boolean): boolean {
  if (prop !== undefined) return prop;
  try {
    return localStorage.getItem('nannos:dev') === '1';
  } catch {
    return false;
  }
}

export interface DevModeValue {
  /** Dev mode is enabled for this panel (prop or localStorage). */
  available: boolean;
  /** Dev chrome is currently shown (header switch; starts on). */
  active: boolean;
  setActive: (active: boolean) => void;
}

const DevModeContext = createContext<DevModeValue>({
  available: false,
  active: false,
  setActive: () => {},
});

/** True while dev chrome should render — what dev-only UI checks. */
export function useDevMode(): boolean {
  const { available, active } = useContext(DevModeContext);
  return available && active;
}

/** The full control surface, for the header switch. */
export function useDevModeControls(): DevModeValue {
  return useContext(DevModeContext);
}

export function DevModeProvider({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const [active, setActive] = useState(true);
  const value = useMemo<DevModeValue>(
    () => ({ available: enabled, active, setActive }),
    [enabled, active],
  );
  return <DevModeContext.Provider value={value}>{children}</DevModeContext.Provider>;
}
