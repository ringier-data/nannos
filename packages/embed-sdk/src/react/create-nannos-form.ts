import { useNannosZodForm, type FormLike } from './use-nannos-form';
import type { ZodObjectLike } from '../core';
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
    });
  };
}
