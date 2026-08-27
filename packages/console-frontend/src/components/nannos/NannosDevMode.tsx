import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useAuth } from '@/contexts/AuthContext';

/**
 * Assistant developer mode for the console.
 *
 * The SDK panel takes a `devMode` prop that turns on its inspector — a live view
 * of what the host pushes to the agent (page context, conversation contextKey,
 * client-object manifest, client-action round trips). That is internal wiring,
 * so the console gates it the same way it gates every other admin surface (see
 * `AdminRoute`): the user must be an administrator AND have admin mode on.
 * Admin mode off hides it again, so a demo never shows the dev chrome by
 * accident.
 *
 * Inside that gate it is an explicit opt-in per browser: an admin should not
 * get an inspector just for being an admin. The choice is stored under the
 * SDK's OWN key (`nannos:dev`), which is the hatch the SDK reads when no prop
 * is given — so the sidebar switch and a hand-set key are the same setting, not
 * two that disagree.
 *
 * The panel receives `enabled` as an explicit boolean, which beats the SDK's
 * localStorage hatch. A non-admin therefore gets a hard `false`: setting the
 * key by hand does nothing.
 */

/** The SDK's own escape-hatch key — see `resolveDevMode` in embed-sdk. */
export const NANNOS_DEV_STORAGE_KEY = 'nannos:dev';

function readStored(): boolean {
  try {
    return localStorage.getItem(NANNOS_DEV_STORAGE_KEY) === '1';
  } catch {
    // Private windows and blocked site data both throw on access.
    return false;
  }
}

export interface NannosDevModeValue {
  /** The user MAY turn it on: administrator with admin mode enabled. */
  available: boolean;
  /** It IS on — what `<AssistantPanel devMode>` gets. False unless available. */
  enabled: boolean;
  toggle: () => void;
}

const NannosDevModeContext = createContext<NannosDevModeValue>({
  available: false,
  enabled: false,
  toggle: () => {},
});

/** The dev-mode gate: for the sidebar switch and for the chat page. */
export function useNannosDevMode(): NannosDevModeValue {
  return useContext(NannosDevModeContext);
}

export function NannosDevModeProvider({ children }: { children: ReactNode }) {
  const { isAdmin, adminMode } = useAuth();
  const [stored, setStored] = useState<boolean>(readStored);

  const available = isAdmin && adminMode;

  const toggle = useCallback(() => {
    setStored((was) => {
      const next = !was;
      try {
        // Removed rather than set to '0': the key's absence is what the SDK's
        // own hatch reads as "off", so we leave storage as the SDK found it.
        if (next) localStorage.setItem(NANNOS_DEV_STORAGE_KEY, '1');
        else localStorage.removeItem(NANNOS_DEV_STORAGE_KEY);
      } catch {
        // Not persisting is survivable; the choice still holds for this tab.
      }
      return next;
    });
  }, []);

  // Cross-tab sync, matching how AuthContext follows admin mode: a developer
  // with the console open twice should not see two different panels.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === NANNOS_DEV_STORAGE_KEY) setStored(event.newValue === '1');
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const value = useMemo<NannosDevModeValue>(
    () => ({ available, enabled: available && stored, toggle }),
    [available, stored, toggle]
  );

  return <NannosDevModeContext.Provider value={value}>{children}</NannosDevModeContext.Provider>;
}
