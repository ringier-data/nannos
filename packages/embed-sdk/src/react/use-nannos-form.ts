import type { ObjectAction } from '../core';
/**
 * `useNannosZodForm` — register a host form as an agent-settable ontology
 * object for the component's lifetime. The low-level hook; most hosts bind
 * through `createNannosForm` (registry-driven id/scope/label derivation).
 */
import { actionsSignature, stableActions } from './stable-actions';
import { useEffect, useRef } from 'react';
import {
  availableFields,
  zodFormRegistration,
  type FieldBridge,
  type FormAdapter,
  type NannosCore,
  type ObjectHandle,
  type Scope,
  type ActionOutcome,
  type ZodObjectLike,
} from '../core';
import { useAssistant } from './provider';

/** The slice of a form we touch. react-hook-form's `UseFormReturn` satisfies it;
 *  so does anything with these two methods. `any` on names/values keeps it
 *  assignable from strongly-typed form libs without variance friction. */
export interface FormLike {
  getValues: (name?: any) => any;
  setValue: (name: any, value: any, options?: any) => void;
  /** Whether the form currently carries a field (see `FormAdapter.has`). Omitted = all. */
  hasField?: (name: string) => boolean;
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
  /** The form's own Save (its button's handler), offered as the approval-gated action
   *  `save`. Read at call time: a fresh function each render does not re-register. Its
   *  presence is part of the registration. */
  save?: () => ActionOutcome | Promise<ActionOutcome>;
  /** What the agent may `invoke` on this form (open a sub-dialog, run a check) — see
   *  `RegisterInput.actions`. Never something that saves. */
  actions?: Record<string, ObjectAction>;
  /** Whether the form holds unsaved edits, the user's included — see
   *  `RegisterInput.isDirty`. Read at call time; its presence is part of the registration. */
  isDirty?: () => boolean;
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
  const { form, type, id, scope, schema, overrides, includeValues, label, setValueOptions, save, actions, isDirty } =
    options;
  const isDirtyRef = useRef(isDirty);
  isDirtyRef.current = isDirty;
  const tracksDirty = !!isDirty;
  const ctxCore = useAssistant().core;
  const core = options.core ?? ctxCore;
  const saveRef = useRef(save);
  saveRef.current = save;
  const savable = !!save;
  const actionsRef = useRef(actions);
  actionsRef.current = actions;
  const actionsSig = actionsSignature(actions);

  // Shape signature: catches field/bridge add/remove (the natural inline-build
  // footgun) without re-registering every render on a fresh object identity.
  // The SERVED fields are part of it: a form that maps fewer fields after a
  // permission or lock change must re-advertise, or the agent sees stale ones.
  const servedSig = form.hasField
    ? availableFields(schema, { has: (f) => form.hasField!(f), get: (f) => form.getValues(f) }, overrides).join(',')
    : '';
  const shapeSig =
    Object.keys(schema.shape).join(',') + '|' + Object.keys(overrides ?? {}).join(',') + '|' + servedSig;

  useEffect(() => {
    if (!core) return;

    const adapter: FormAdapter = {
      get: (field) => form.getValues(field),
      set: (field, value) => form.setValue(field, value, setValueOptions ?? DEFAULT_SET_OPTIONS),
      snapshot: () => form.getValues() as Record<string, unknown>,
      ...(form.hasField ? { has: (field: string) => form.hasField!(field) } : {}),
    };

    const handle: ObjectHandle = core.register(
      zodFormRegistration<TState>({
        type,
        id,
        scope,
        schema,
        adapter,
        overrides,
        includeValues,
        label,
        ...(savable ? { save: () => saveRef.current?.() } : {}),
        ...(tracksDirty ? { isDirty: () => isDirtyRef.current?.() === true } : {}),
        actions: stableActions(actionsRef),
      }),
    );
    return () => handle.dispose();
    // Re-register on identifying inputs + the schema/override SHAPE (shapeSig).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [core, form, type, id, scope, includeValues, label, shapeSig, savable, actionsSig, tracksDirty]);
}
