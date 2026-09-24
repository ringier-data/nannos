import { useEffect, useState } from 'react';

/**
 * Debounce a value before it reaches a server-side query.
 *
 * Search boxes back paginated endpoints, so every keystroke would otherwise be a
 * request.
 *
 * Callers reset their page to the first one from their input handler, on the raw
 * value — so the reset is immediate while the term lags by `delayMs`. Typing the
 * first character from page > 1 therefore fires one request for
 * `{page: 1, <previous term>}` that the debounced term supersedes ~`delayMs`
 * later; `keepPreviousData` hides it, and it costs one query.
 *
 * Collapsing that would mean resetting on the debounced edge instead, which
 * needs the page state and the term to live together — see the `usePagedSearch`
 * follow-up rather than reaching for an effect here (`react-hooks/
 * set-state-in-effect`).
 */
export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}
