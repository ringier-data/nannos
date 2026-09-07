/**
 * Send mode — what Enter does while a turn is already RUNNING.
 *
 *  - `steer` (default) — the message joins the running turn: the agent reads it
 *    mid-flight and adjusts. Nothing is interrupted.
 *  - `stop-and-send` — the running turn is cancelled first, then the message
 *    starts a fresh turn. For when the answer under way is simply the wrong one.
 *
 * With no turn running both modes are a plain send, so the composer only shows
 * the control while the agent is busy. The choice is a per-viewer convenience:
 * it lives in `localStorage` and never leaves the browser (same shape as
 * `apply-mode.tsx`).
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

export type SendMode = 'steer' | 'stop-and-send';

const STORAGE_KEY = 'nannos:sendMode';

export function readStoredSendMode(): SendMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'stop-and-send' ? 'stop-and-send' : 'steer';
  } catch {
    // Private windows and blocked site data both throw on access.
    return 'steer';
  }
}

export interface SendModeValue {
  mode: SendMode;
  setMode: (mode: SendMode) => void;
}

const SendModeContext = createContext<SendModeValue>({ mode: 'steer', setMode: () => {} });

export function useSendMode(): SendModeValue {
  return useContext(SendModeContext);
}

export function SendModeProvider({ children }: { children: ReactNode }) {
  const [mode, setStored] = useState<SendMode>(readStoredSendMode);
  const setMode = useCallback((next: SendMode) => {
    setStored(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Not persistable here; the choice still holds for this mount.
    }
  }, []);
  const value = useMemo<SendModeValue>(() => ({ mode, setMode }), [mode, setMode]);
  return <SendModeContext.Provider value={value}>{children}</SendModeContext.Provider>;
}
