import { useState } from 'react';

/**
 * A page number that belongs to one dataset, and reads as 1 for any other.
 *
 * When a list switches to a different dataset without the user asking — admin
 * mode toggled, a scope that is no longer offered falling back to another —
 * the old page number would be carried over into a list that may not have that
 * many pages, rendering an empty table with "Page 3 of 1". Resetting it in an
 * effect costs an extra render and trips `react-hooks/set-state-in-effect`, so
 * the page is stored alongside the key it was chosen for instead: a key change
 * is itself the reset.
 */
export function useKeyedPage(key: string): [number, (page: number) => void] {
  const [state, setState] = useState({ key, page: 1 });
  const page = state.key === key ? state.page : 1;
  const setPage = (next: number) => setState({ key, page: next });
  return [page, setPage];
}
