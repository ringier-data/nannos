import { useCallback, useEffect, useState } from 'react';

const STORAGE_PREFIX = 'a2a-';

/**
 * Hook for persisting state to localStorage
 */
export function useLocalStorage<T>(key: string, initialValue: T): [T, (value: T | ((prev: T) => T)) => void] {
  const storageKey = `${STORAGE_PREFIX}${key}`;

  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      const item = localStorage.getItem(storageKey);
      return item ? JSON.parse(item) : initialValue;
    } catch {
      return initialValue;
    }
  });

  const setValue = useCallback(
    (value: T | ((prev: T) => T)) => {
      try {
        const valueToStore = value instanceof Function ? value(storedValue) : value;
        setStoredValue(valueToStore);
        localStorage.setItem(storageKey, JSON.stringify(valueToStore));
      } catch (error) {
        console.error(`Failed to save to localStorage: ${storageKey}`, error);
      }
    },
    [storageKey, storedValue]
  );

  return [storedValue, setValue];
}

/**
 * Hook for managing session ID
 */
export function useSessionId(): string {
  const [sessionId] = useLocalStorage<string>('session-id', '');

  useEffect(() => {
    if (!sessionId) {
      const newSessionId = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        const v = c === 'x' ? r : (r & 0x3) | 0x8;
        return v.toString(16);
      });
      localStorage.setItem(`${STORAGE_PREFIX}session-id`, JSON.stringify(newSessionId));
    }
  }, [sessionId]);

  return sessionId || localStorage.getItem(`${STORAGE_PREFIX}session-id`)?.replace(/"/g, '') || '';
}

/**
 * Hook for panel resize persistence
 */
export function usePanelSize(panelKey: string, defaultSize: string): [string, (size: string) => void] {
  const storageKey = `ui.${panelKey}Width`;

  const [size, setSize] = useState<string>(() => {
    try {
      return localStorage.getItem(storageKey) || defaultSize;
    } catch {
      return defaultSize;
    }
  });

  const updateSize = useCallback(
    (newSize: string) => {
      setSize(newSize);
      try {
        localStorage.setItem(storageKey, newSize);
      } catch (error) {
        console.error(`Failed to save panel size: ${storageKey}`, error);
      }
    },
    [storageKey]
  );

  return [size, updateSize];
}
