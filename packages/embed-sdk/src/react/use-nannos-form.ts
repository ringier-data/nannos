/**
 * `useNannosZodForm` — register a host form as an agent-settable ontology
 * object for the component's lifetime. The low-level hook; most hosts bind
 * through `createNannosForm` (registry-driven id/scope/label derivation).
 */
import { useEffect } from 'react';
import {
  zodFormRegistration,
  type FieldBridge,
  type FormAdapter,
  type NannosCore,
  type ObjectHandle,
  type Scope,
  type ZodObjectLike,
} from '../core';
import { useAssistant } from './provider';

/** The slice of a form we touch. react-hook-form's `UseFormReturn` satisfies it;
 *  so does anything with these two methods. `any` on names/values keeps it
 *  assignable from strongly-typed form libs without variance friction. */
export interface FormLike {
  getValues: (name?: any) => any;
  setValue: (name: any, value: any, options?: any) => void;
}

export interface UseNannosZodFormOptions<TState> {
  /** The host form (react-hook-form `UseFormReturn`, or any getValues/setValue pair). */
  form: FormLike;
  type: string;
  id: string;
  scope: Scope;
  /** Zod object schema — drives fields, validation, and derived field specs. */
  schema: ZodObjectLike;
  /** Bridges for fields with no 1:1 form key (e.g. dates ↔ a tuple). */
  overrides?: Record<string, FieldBridge>;
  includeValues?: boolean;
  label?: string;
  /** setValue options (default: dirty + validate + touch, so it behaves as if typed). */
  setValueOptions?: unknown;
  /** Override the core from context (rarely needed — <NannosProvider> supplies it). */
  core?: NannosCore | null;
}

const DEFAULT_SET_OPTIONS = { shouldDirty: true, shouldValidate: true, shouldTouch: true };

/**
 * Registers on mount, disposes on unmount; no-ops without a provider or while
 * disabled (null core). Writes go through the form's own `setValue`
 * (dirty/validate/touch by default) so the user still reviews and saves.
 *
 * `schema`/`overrides` are read at registration. Re-registration is triggered
 * by a SHAPE signature (schema field names + bridge keys), so adding/removing
 * a field or a bridge takes effect even if you build them inline. It does NOT
 * deep-compare VALUES — changing a bridge's `read`/`write` body while keeping
 * the same keys won't re-register; keep bridge bodies stable (module constant)
 * or change a key to force it.
 */
export function useNannosZodForm<TState = Record<string, unknown>>(
  options: UseNannosZodFormOptions<TState>,
): void {
  const { form, type, id, scope, schema, overrides, includeValues, label, setValueOptions } = options;
  const ctxCore = useAssistant().core;
  const core = options.core ?? ctxCore;

  // Shape signature: catches field/bridge add/remove (the natural inline-build
  // footgun) without re-registering every render on a fresh object identity.
  const shapeSig =
    Object.keys(schema.shape).join(',') + '|' + Object.keys(overrides ?? {}).join(',');

  useEffect(() => {
    if (!core) return;

    const adapter: FormAdapter = {
      get: (field) => form.getValues(field),
      set: (field, value) => form.setValue(field, value, setValueOptions ?? DEFAULT_SET_OPTIONS),
      snapshot: () => form.getValues() as Record<string, unknown>,
    };

    const handle: ObjectHandle = core.register(
      zodFormRegistration<TState>({ type, id, scope, schema, adapter, overrides, includeValues, label }),
    );
    return () => handle.dispose();
    // Re-register on identifying inputs + the schema/override SHAPE (shapeSig).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [core, form, type, id, scope, includeValues, label, shapeSig]);
}
