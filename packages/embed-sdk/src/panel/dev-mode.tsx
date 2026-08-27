/**
 * Developer-mode flag, shared through context so the thread, header and
 * inspector can all react to it without prop drilling.
 *
 * Two levels: dev mode is AVAILABLE (the panel's `devMode` prop, or — without
 * a host rebuild — `localStorage['nannos:dev']='1'`), and while available it
 * can be flipped ACTIVE/inactive live from the inspector's own bar, so the
 * developer can preview the exact end-user view without leaving dev mode.
 *
 * ACTIVE is remembered per browser. It has to be: on a surface the host mounts
 * and unmounts (the console's chat page is a route), plain state would put the
 * dev chrome back every time the developer navigated to it. Absence reads as
 * ON, so a host that just switched dev mode on sees it without hunting for the
 * switch.
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

/** `devMode` prop wins when set; otherwise the localStorage escape hatch. */
export function resolveDevMode(prop?: boolean): boolean {
  if (prop !== undefined) return prop;
  try {
    return localStorage.getItem('nannos:dev') === '1';
  } catch {
    return false;
  }
}

/** Where the ACTIVE half is remembered. `'0'` = previewing the end-user view;
 *  anything else (including no value at all) = dev chrome on. */
const ACTIVE_STORAGE_KEY = 'nannos:dev:active';

function readActive(): boolean {
  try {
    return localStorage.getItem(ACTIVE_STORAGE_KEY) !== '0';
  } catch {
    // Private windows and blocked site data both throw on access.
    return true;
  }
}

export interface DevModeValue {
  /** Dev mode is enabled for this panel (prop or localStorage). */
  available: boolean;
  /** Dev chrome is currently shown (the inspector's switch; starts on). */
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
  const [active, setActiveState] = useState(readActive);
  const setActive = useCallback((next: boolean) => {
    setActiveState(next);
    try {
      // Only the OFF state is written: absence is the default, so a browser
      // that never touched the switch keeps reading as on.
      if (next) localStorage.removeItem(ACTIVE_STORAGE_KEY);
      else localStorage.setItem(ACTIVE_STORAGE_KEY, '0');
    } catch {
      // Not persisting is survivable; the choice still holds for this mount.
    }
  }, []);
  const value = useMemo<DevModeValue>(
    () => ({ available: enabled, active, setActive }),
    [enabled, active],
  );
  return <DevModeContext.Provider value={value}>{children}</DevModeContext.Provider>;
}
