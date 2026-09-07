// Zod-driven form instrumentation. Turns a Zod object schema (+ a tiny form
// adapter, + optional bridges for fields Zod can't map 1:1) into a ready-to-
// register ontology object — so a host stops hand-writing the field list, the
// fieldSpec mapping, the per-field validate-and-apply loop, and the getState merge.
//
// Zod 4 is a peerDependency, so this SDK and the host share ONE Zod instance —
// which means we derive the agent-facing FieldSpecs here (via `z.toJSONSchema`) and
// the host passes only the schema. Validation in `apply` uses the schema's own
// `.shape[f].safeParse`.

import { z } from 'zod';
import type { ApplyResult, FieldSpec, RegisterInput, Scope } from './types';

/** Minimal read/write view of the host's form, by field name. Framework-free:
 *  react-hook-form → `{ get: f => form.getValues(f), set: (f,v) => form.setValue(f,v,opts), snapshot: () => form.getValues() }`. */
export interface FormAdapter {
  get: (field: string) => unknown;
  set: (field: string, value: unknown) => void;
  /** All current field values (for includeValues / getState). */
  snapshot: () => Record<string, unknown>;
}

/** Bridge for a field with no clean 1:1 form key (e.g. two ISO dates ↔ a
 *  `[Moment, Moment]` tuple). Reads the current value out and writes an
 *  agent-provided (already schema-validated) value in, via the adapter. */
export interface FieldBridge {
  read: (adapter: FormAdapter) => unknown;
  write: (value: unknown, adapter: FormAdapter) => void;
}

/** Structural view of a Zod object — just what we call on it (its own methods),
 *  so it works regardless of which Zod copy/version built the schema. */
export interface ZodObjectLike {
  shape: Record<string, { safeParse: (value: unknown) => { success: boolean; data?: unknown } }>;
}

type JsonProp = {
  type?: string | string[];
  enum?: unknown[];
  description?: string;
  anyOf?: Array<{ type?: string }>;
};

/**
 * Map a JSON schema (from the host's `z.toJSONSchema(schema)`) to the compact
 * FieldSpec[] the agent sees — pulling type, enum values, and description, and
 * unwrapping nullable `anyOf: [{type}, {type:'null'}]`. Pure: takes a plain
 * object, no Zod dependency.
 */
export function jsonSchemaToFieldSpecs(jsonSchema: unknown): FieldSpec[] {
  // Loose input: a plain JSON schema (`{ properties }`). Typed `unknown` so any
  // Zod version's `z.toJSONSchema(...)` output is accepted without coupling the SDK
  // to Zod's exported JSONSchema type (which also permits boolean subschemas).
  const properties = (jsonSchema as { properties?: Record<string, unknown> } | null | undefined)?.properties;
  if (!properties) return [];
  return Object.entries(properties).map(([name, raw]) => {
    if (!raw || typeof raw !== 'object') return { name };
    const p = raw as JsonProp;
    const enumVals = Array.isArray(p.enum) ? p.enum.map(String) : undefined;
    const anyOfType = p.anyOf?.map((o) => o?.type).find((t) => t && t !== 'null');
    const rawType = (Array.isArray(p.type) ? p.type.find((t) => t !== 'null') : p.type) ?? anyOfType;
    return {
      name,
      type: enumVals ? 'enum' : rawType,
      ...(enumVals ? { enum: enumVals } : {}),
      ...(p.description ? { description: p.description } : {}),
    };
  });
}

/**
 * Derive FieldSpecs directly from a Zod object schema (type/enum/description per
 * field). Uses the shared Zod's `z.toJSONSchema`; on any failure, degrades to bare
 * field names (still valid — the manifest also carries the schema).
 */
export function zodToFieldSpecs(schema: ZodObjectLike): FieldSpec[] {
  try {
    return jsonSchemaToFieldSpecs(z.toJSONSchema(schema as unknown as z.ZodType));
  } catch {
    return Object.keys(schema.shape).map((name) => ({ name }));
  }
}

export interface ZodFormRegistrationInput<TState> {
  type: string;
  id: string;
  scope: Scope;
  label?: string;
  includeValues?: boolean;
  /** The Zod object schema — drives the field list, per-field validation, AND
   *  (unless `fieldSpecs` is given) the typed field descriptors via z.toJSONSchema. */
  schema: ZodObjectLike;
  /** Override the auto-derived field descriptors. Rarely needed — omit and the SDK
   *  derives them from `schema`. */
  fieldSpecs?: FieldSpec[];
  /** How to read/write the underlying form. */
  adapter: FormAdapter;
  /** Bridges for fields with no 1:1 form key (keyed by schema field name). */
  overrides?: Record<string, FieldBridge>;
}

/**
 * Build a `RegisterInput` from a Zod schema + a form adapter. The host declares
 * only what's app-specific (the schema, and bridges for the odd non-1:1 field);
 * this derives the field list, wires per-field validation into `apply`, routes
 * bridged fields through their override and everything else through a plain
 * `adapter.set`, and assembles `getState` bounded to the schema contract.
 *
 *   nannos.register(zodFormRegistration({ type, id, scope, schema, adapter, overrides }))
 */
export function zodFormRegistration<TState = Record<string, unknown>>(
  input: ZodFormRegistrationInput<TState>,
): RegisterInput<TState> {
  const overrides = input.overrides ?? {};
  // The contract = the schema's fields ∪ any bridge keys (normally a subset).
  const fields = [...new Set([...Object.keys(input.schema.shape), ...Object.keys(overrides)])];

  return {
    type: input.type,
    id: input.id,
    scope: input.scope,
    label: input.label,
    schema: input.schema,
    fields,
    fieldSpecs: input.fieldSpecs ?? zodToFieldSpecs(input.schema),
    includeValues: input.includeValues,
    getState: () => {
      // Project to the CONTRACT (declared fields + bridge reads) — never the raw
      // form snapshot. The schema is the agent-settable boundary; sending
      // `{...snapshot()}` would leak every undeclared form field and non-plain
      // values (e.g. a `[Moment, Moment]` tuple behind a bridged date) past it.
      const state: Record<string, unknown> = {};
      for (const field of fields) {
        const bridge = overrides[field];
        state[field] = bridge ? bridge.read(input.adapter) : input.adapter.get(field);
      }
      return state as TState;
    },
    apply: (values): ApplyResult => {
      // Validate each field INDEPENDENTLY against the schema before writing, so one
      // mis-guessed field can't block the valid ones or corrupt the form. Bridged
      // fields go through their override; the rest are a plain adapter.set. Rejected
      // fields are reported (not silently swallowed) so the widget/agent can react.
      const source = values as Record<string, unknown>;
      const applied: string[] = [];
      const rejected: ApplyResult['rejected'] = [];
      for (const [field, fieldSchema] of Object.entries(input.schema.shape)) {
        if (!(field in source)) continue;
        const result = fieldSchema.safeParse(source[field]);
        if (!result.success || result.data === undefined) {
          rejected.push({ field, reason: 'failed schema validation' });
          continue;
        }
        const bridge = overrides[field];
        if (bridge) bridge.write(result.data, input.adapter);
        else input.adapter.set(field, result.data);
        applied.push(field);
      }
      return { applied, rejected };
    },
  };
}
