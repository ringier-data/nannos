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

/** One form field held in its own `useState`: the value and its setter. */
export type StateField<V = any> = readonly [value: V, set: (next: V) => void];

/**
 * Bind a form whose fields each live in their own `useState` to the `FormLike`
 * seam — the common shape of hand-rolled forms:
 *
 * ```ts
 * const form = useStateFieldsAdapter({ name: [name, setName], description: [description, setDescription] });
 * useNannosForm({ form, type: 'Catalog', id });
 * ```
 *
 * Stable identity like `useObjectStateAdapter` (reads go through a ref). A write
 * to a field the map doesn't carry is ignored, and `getValues()` returns only
 * the mapped fields. `hasField` tells the registration which fields are mapped,
 * so a schema field left out of the map (not editable for this user, locked by
 * a host) is not advertised to the agent and a write to it is reported rejected.
 * Build the map conditionally to follow permissions — it re-registers on change.
 */
export function useStateFieldsAdapter(fields: Record<string, StateField>): FormLike {
  const fieldsRef = useRef(fields);
  fieldsRef.current = fields;

  return useMemo(
    () => ({
      getValues: (name?: string) => {
        const current = fieldsRef.current;
        if (name !== undefined) return current[name]?.[0];
        return Object.fromEntries(Object.entries(current).map(([key, [value]]) => [key, value]));
      },
      setValue: (name: string, value: unknown) => {
        fieldsRef.current[name]?.[1](value);
      },
      hasField: (name: string) => Object.prototype.hasOwnProperty.call(fieldsRef.current, name),
    }),
    []
  );
}
