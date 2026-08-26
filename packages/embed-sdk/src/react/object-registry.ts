import type { FieldBridge, Scope, ZodObjectLike } from '../core';

/**
 * The host-side declaration of agent-settable object types. One entry per
 * ontology type carries the whole contract — schema, bridges, how the route's id
 * maps to a manifest id, and the labels `highlight` needs for fields with no
 * `[name]` in the DOM. Everything mechanical (scope, id string, manifest label)
 * is derived from it, so binding a form at the call site carries no per-type logic.
 *
 * The registry is passed explicitly rather than populated by a module side
 * effect: `sideEffects` in package.json marks app modules pure, so a bare
 * `registerObjectTypes(...)` call in a barrel is legal for the bundler to drop —
 * which would silently ship every form with an empty schema in production while
 * working fine in `npm start`.
 *
 * Deliberately app-agnostic: each host declares its own entries.
 * Absorbed verbatim from cockpit-frontend src/nannos/host/formRegistry.ts (v2 rewrite).
 */

/**
 * How a route's id parameter maps onto "does this object already exist".
 * - `simple-numeric` — numeric id, absent on the create route (`Campaign`).
 * - `simple-string` — opaque string id, absent on the create route (`Topic`).
 * - `simple-sentinel` — the create route reuses the details page with a literal
 *   sentinel in place of the id (`CampaignPlan`, where it is `'create'`).
 * - `nested` — a child of another entity; the manifest id is `parent/child`.
 */
export type IdShape = 'simple-numeric' | 'simple-string' | 'simple-sentinel' | 'nested';

export type RouteId = string | number | null | undefined;

export interface ManifestLabelContext {
  singular: string;
  parentSingular: string;
  id: RouteId;
  parentId: RouteId;
  isExisting: boolean;
}

export interface ObjectTypeDefinition {
  /** Zod object schema — the agent-settable contract for this type. */
  schema: ZodObjectLike;
  /** Bridges for fields with no 1:1 form key, keyed by schema field name. */
  overrides?: Record<string, FieldBridge>;
  /** Human name used to build the manifest label, e.g. `'Extra cost'`. */
  singular: string;
  idShape: IdShape;
  /** Parent name for `nested` labels — e.g. `'campaign'` → `Theme 5 (campaign 12)`. */
  parentSingular?: string;
  /** Literal the create route uses in place of the id (`idShape: 'simple-sentinel'`). */
  createSentinel?: string;
  /**
   * Fields with no react-hook-form `[name]` in the DOM (dates, selects,
   * autocompletes, bridged fields) — matched by their MUI label text so
   * `highlight` can find them.
   */
  highlightLabels?: Record<string, string>;
  /** Override the derived manifest label, for types the default shape doesn't fit. */
  label?: (ctx: ManifestLabelContext) => string;
}

export type ObjectTypeRegistry = Record<string, ObjectTypeDefinition>;

/**
 * Label text for a field that carries no `[name]` in the DOM.
 *
 * A registered type answers only for itself: guessing across types would let a
 * highlight on one form resolve to another form's label and outline the wrong
 * control — and forms of different types DO co-exist (the campaign-plan page
 * mounts the settings and briefing steps together). Only an unknown type scans
 * everything, which is the best it can do.
 */
export function resolveHighlightLabel(
  registry: ObjectTypeRegistry,
  type: string | undefined,
  field: string
): string | undefined {
  const definition = type ? registry[type] : undefined;
  if (definition) return definition.highlightLabels?.[field];
  if (type) return undefined;
  for (const candidate of Object.values(registry)) {
    const label = candidate.highlightLabels?.[field];
    if (label) return label;
  }
  return undefined;
}

export function isExistingId(definition: ObjectTypeDefinition, id: RouteId): boolean {
  switch (definition.idShape) {
    case 'simple-string':
      return !!id;
    case 'simple-sentinel':
      return !!id && id !== (definition.createSentinel ?? 'create') && !Number.isNaN(Number(id));
    default:
      // Numeric ids: `Number(null)` is 0, not NaN, so null must be excluded explicitly.
      return id !== null && id !== undefined && !Number.isNaN(Number(id));
  }
}

export function deriveScope(isExisting: boolean): Scope {
  return isExisting ? 'update' : 'create';
}

export function deriveObjectId(definition: ObjectTypeDefinition, id: RouteId, parentId: RouteId): string {
  if (definition.idShape === 'nested') {
    return `${parentId ?? 'unknown'}/${isExistingId(definition, id) ? id : 'new'}`;
  }
  return isExistingId(definition, id) ? String(id) : 'new';
}

const lowerFirst = (value: string): string => value.charAt(0).toLowerCase() + value.slice(1);

export function deriveManifestLabel(definition: ObjectTypeDefinition, id: RouteId, parentId: RouteId): string {
  const isExisting = isExistingId(definition, id);
  const parentSingular = definition.parentSingular ?? 'parent';
  if (definition.label) {
    return definition.label({ singular: definition.singular, parentSingular, id, parentId, isExisting });
  }
  if (definition.idShape === 'nested') {
    const suffix = `(${parentSingular} ${parentId})`;
    return isExisting ? `${definition.singular} ${id} ${suffix}` : `New ${lowerFirst(definition.singular)} ${suffix}`;
  }
  return isExisting ? `${definition.singular} ${id}` : `New ${lowerFirst(definition.singular)}`;
}
