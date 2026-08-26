import { useMemo, useRef } from 'react';
import type { FormLike } from './use-nannos-form';

/**
 * Bind a plain `{ state, patch }` container to the SDK's `FormLike` seam, for
 * screens that are NOT react-hook-form (react-hook-form's `UseFormReturn`
 * already satisfies `FormLike` as-is). Example (cockpit's Targeting Engine Audience page): `useAudiences` hands out an `audience` object plus
 * `setAudience(Partial<Audience>)`.
 *
 * Identity is stable for the component's lifetime and reads go through refs:
 * `useNannosZodForm` lists `form` in its effect deps, so returning a fresh
 * object each render would re-register the ontology object on every keystroke.
 */
export function useObjectStateAdapter<T extends Record<string, unknown>>(
  state: T | undefined,
  patch: (next: Partial<T>) => void
): FormLike {
  const stateRef = useRef(state);
  stateRef.current = state;
  const patchRef = useRef(patch);
  patchRef.current = patch;

  return useMemo(
    () => ({
      getValues: (name?: string) => (name === undefined ? stateRef.current : stateRef.current?.[name]),
      setValue: (name: string, value: unknown) => patchRef.current({ [name]: value } as Partial<T>),
    }),
    []
  );
}
