import type { ManifestEntry, ObjectHandle, RegisterInput } from './types';

/**
 * Registry of on-screen ontology objects. The agent only ever learns of objects
 * the host explicitly registered here — this is the trust boundary for
 * client-action (the widget can never touch anything not in this map).
 */
export class ObjectRegistry {
  private readonly objects = new Map<string, RegisterInput>();
  private readonly listeners = new Set<() => void>();

  register<TState>(input: RegisterInput<TState>): ObjectHandle {
    const key = `${input.type}:${input.id}`;
    this.objects.set(key, input as RegisterInput);
    this.emit();
    return {
      key,
      dispose: () => {
        this.objects.delete(key);
        this.emit();
      },
    };
  }

  get(type: string, id: string): RegisterInput | undefined {
    return this.objects.get(`${type}:${id}`);
  }

  /** Compact index pushed with each turn — progressive disclosure: no schema, no state. */
  manifest(): ManifestEntry[] {
    return [...this.objects.values()].map((o) => {
      const { type, id, scope, label, fields, fieldSpecs, includeValues, getState } = o;
      const entry: ManifestEntry = {
        type,
        id,
        scope,
        ...(label ? { label } : {}),
        ...(fields?.length ? { fields } : {}),
        ...(fieldSpecs?.length ? { fieldSpecs } : {}),
      };
      // Opt-in current values, restricted to the declared fields (compact +
      // avoids leaking undeclared state). Non-empty values only.
      if (includeValues) {
        try {
          const state = (getState() ?? {}) as Record<string, unknown>;
          const keys = fieldSpecs?.map((f) => f.name) ?? fields ?? Object.keys(state);
          const values: Record<string, unknown> = {};
          for (const k of keys) {
            const v = state[k];
            if (v !== undefined && v !== null && v !== '') values[k] = v;
          }
          if (Object.keys(values).length > 0) entry.values = values;
        } catch {
          /* getState may throw mid-render; just omit values this turn */
        }
      }
      return entry;
    });
  }

  /** Pulled on demand when the agent decides to engage a specific object. */
  detail(type: string, id: string): { schema?: unknown; state?: unknown } | undefined {
    const o = this.get(type, id);
    if (!o) return undefined;
    return { schema: o.schema, state: o.getState() };
  }

  onChange(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit() {
    for (const fn of this.listeners) fn();
  }
}
