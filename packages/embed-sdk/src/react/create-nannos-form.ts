import { useEffect, useRef } from 'react';
import { useNannosZodForm, type FormLike } from './use-nannos-form';
import type { ObjectAction, ActionOutcome, ZodObjectLike } from '../core';
import { useAssistant } from './provider';
import { actionsSignature, stableActions } from './stable-actions';
import {
  deriveManifestLabel,
  deriveObjectId,
  deriveScope,
  isExistingId,
  type ObjectTypeRegistry,
  type RouteId,
} from './object-registry';

export interface UseNannosFormOptions {
  /** react-hook-form's `UseFormReturn`, or any `getValues`/`setValue` pair
   *  (see `useObjectStateAdapter` for plain state containers). */
  form: FormLike;
  /** Registered ontology type, e.g. `'Campaign'`. */
  type: string;
  /** The route's id for this object — absent/sentinel on a create route. */
  id: RouteId;
  /** Parent id, for `nested` types (e.g. the campaign a theme belongs to). */
  parentId?: RouteId;
  includeValues?: boolean;
  /** The form's own Save — see `UseNannosZodFormOptions.save`. */
  save?: () => ActionOutcome | Promise<ActionOutcome>;
  /** What the agent may `invoke` on this form — see `UseNannosZodFormOptions.actions`. */
  actions?: Record<string, ObjectAction>;
  /** Whether the form holds unsaved edits — see `UseNannosZodFormOptions.isDirty`. */
  isDirty?: () => boolean;
}

/** Registration degrades to a no-op object rather than throwing mid-render. */
const EMPTY_SCHEMA: ZodObjectLike = { shape: {} };

/**
 * Build the app's form-binding hook from its object-type registry.
 *
 * A factory rather than a hook reading a global: the returned hook closes over
 * the registry, so the bundler can't separate the two. Scope, manifest id and
 * label are derived from the type's `idShape` (see `formRegistry.ts`), which is
 * why a call site carries no per-type logic — adding a surface costs one line
 * plus one registry entry.
 *
 * The hook no-ops when the Nannos provider is disabled or absent (null core).
 */
export function createNannosForm(registry: ObjectTypeRegistry) {
  return function useNannosForm<TState = Record<string, unknown>>({
    form,
    type,
    id,
    parentId,
    includeValues = true,
    save,
    actions,
    isDirty,
  }: UseNannosFormOptions): void {
    const definition = registry[type];

    // Dev-only nag, without a Node types dependency (hosts define NODE_ENV via
    // their bundler; a runtime with neither just skips the warning).
    const nodeEnv = (globalThis as { process?: { env?: { NODE_ENV?: string } } }).process?.env
      ?.NODE_ENV;
    if (!definition && nodeEnv !== 'production') {
      // eslint-disable-next-line no-console
      console.error(`[nannos] no object type registered for "${type}" — check the registry passed to createNannosForm`);
    }

    useNannosZodForm<TState>({
      form,
      type,
      id: definition ? deriveObjectId(definition, id, parentId) : 'new',
      scope: deriveScope(definition ? isExistingId(definition, id) : false),
      schema: definition?.schema ?? EMPTY_SCHEMA,
      overrides: definition?.overrides,
      includeValues,
      label: definition ? deriveManifestLabel(definition, id, parentId) : type,
      save,
      actions,
      isDirty,
    });
  };
}

/**
 * The view-mode counterpart of `createNannosForm`: registers a page's ACTIONS for an
 * object shown read-only (a detail page before Edit, a list with a "New" button), so
 * the agent can do what a click does — enter edit mode, open a dialog — and then fill
 * the form that opens. Same type/id derivation as the form, so a view registration and
 * the form that replaces it carry the same `type:id`; mount it only while the form is
 * NOT mounted (one object per key).
 */
export function createNannosActions(registry: ObjectTypeRegistry) {
  return function useNannosActions({
    type,
    id,
    parentId,
    actions,
  }: {
    type: string;
    id: RouteId;
    parentId?: RouteId;
    actions: Record<string, ObjectAction>;
  }): void {
    const definition = registry[type];
    const core = useAssistant().core;
    const actionsRef = useRef<Record<string, ObjectAction> | undefined>(actions);
    actionsRef.current = actions;
    const sig = actionsSignature(actions);
    const objectId = definition ? deriveObjectId(definition, id, parentId) : 'new';
    const label = definition ? deriveManifestLabel(definition, id, parentId) : type;
    useEffect(() => {
      if (!core) return;
      const handle = core.register({
        type,
        id: objectId,
        scope: 'view',
        label,
        getState: () => ({}),
        // Read-only here: the fields are filled after the action that opens the form —
        // or never, when the host registered the object with no actions at all.
        apply: (values) => ({
          applied: [],
          rejected: Object.keys(values as Record<string, unknown>).map((field) => ({
            field,
            reason: Object.keys(actionsRef.current ?? {}).length
              ? 'read-only here — invoke the action that opens the form first'
              : 'read-only here — the user cannot change this object on this page',
          })),
        }),
        actions: stableActions(actionsRef),
      });
      return () => handle.dispose();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [core, type, objectId, label, sig]);
  };
}
