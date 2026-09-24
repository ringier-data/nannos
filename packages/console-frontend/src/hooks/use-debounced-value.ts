import { useEffect, useState } from 'react';

/**
 * Debounce a value before it reaches a server-side query.
 *
 * Search boxes back paginated endpoints, so every keystroke would otherwise be a
 * request. Callers reset their page to the first one when the debounced value
 * changes, not when the raw input does.
 */
export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}
