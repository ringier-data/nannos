import { describe, it, expect, vi } from 'vitest';
import { z } from 'zod';
import { jsonSchemaToFieldSpecs, zodFormRegistration, zodToFieldSpecs, type FormAdapter } from './zod-form';

describe('jsonSchemaToFieldSpecs', () => {
  it('maps type, enum, description and unwraps nullable anyOf', () => {
    const specs = jsonSchemaToFieldSpecs({
      properties: {
        name: { type: 'string', description: 'Campaign name' },
        kpi: { enum: ['CPC', 'CPA'], description: 'KPI' },
        maxUnitPrice: { anyOf: [{ type: 'number' }, { type: 'null' }], description: 'Max price' },
        count: { type: ['integer', 'null'] },
      },
    });
    expect(specs).toContainEqual({ name: 'name', type: 'string', description: 'Campaign name' });
    expect(specs).toContainEqual({ name: 'kpi', type: 'enum', enum: ['CPC', 'CPA'], description: 'KPI' });
    expect(specs).toContainEqual({ name: 'maxUnitPrice', type: 'number', description: 'Max price' });
    expect(specs.find((s) => s.name === 'count')?.type).toBe('integer');
  });

  it('returns [] for an empty/absent schema', () => {
    expect(jsonSchemaToFieldSpecs(undefined)).toEqual([]);
    expect(jsonSchemaToFieldSpecs({})).toEqual([]);
  });
});

describe('zodToFieldSpecs (derives from a Zod schema via the shared Zod)', () => {
  it('pulls type / enum / description straight from the schema', () => {
    const schema = z.object({
      name: z.string().describe('Campaign name'),
      kpi: z.enum(['CPC', 'CPA']).describe('KPI'),
      budget: z.coerce.string(),
    });
    const specs = zodToFieldSpecs(schema);
    expect(specs.find((s) => s.name === 'name')).toMatchObject({ type: 'string', description: 'Campaign name' });
    expect(specs.find((s) => s.name === 'kpi')).toMatchObject({ type: 'enum', enum: ['CPC', 'CPA'] });
    expect(specs.find((s) => s.name === 'budget')?.type).toBe('string');
  });
});

describe('zodFormRegistration', () => {
  const schema = z.object({
    name: z.string(),
    budget: z.coerce.string(),
    // A field with no 1:1 form key — bridged below.
    startDate: z.string(),
  });

  // In-memory form: the "real" field is `duration: [start, end]`; startDate bridges to slot 0.
  function makeAdapter(initial: Record<string, unknown> = {}): FormAdapter & { store: Record<string, unknown> } {
    const store: Record<string, unknown> = { duration: [null, null], ...initial };
    return {
      store,
      get: (f) => store[f],
      set: (f, v) => {
        store[f] = v;
      },
      snapshot: () => ({ ...store }),
    };
  }

  it('derives fields, validates per-field, and routes bridged vs direct writes', () => {
    const adapter = makeAdapter();
    const reg = zodFormRegistration({
      type: 'Campaign',
      id: 'new',
      scope: 'create',
      schema,
      adapter,
      overrides: {
        startDate: {
          read: (a) => (a.get('duration') as [string | null, string | null])?.[0] ?? null,
          write: (v, a) => {
            const [, end] = (a.get('duration') as [string | null, string | null]) ?? [null, null];
            a.set('duration', [v as string, end ?? null]);
          },
        },
      },
    });

    expect(reg.fields).toEqual(['name', 'budget', 'startDate']);
    // fieldSpecs auto-derived from the schema (no fieldSpecs passed).
    expect(reg.fieldSpecs?.map((s) => s.name)).toEqual(['name', 'budget', 'startDate']);

    // Direct fields validated + set; bridged field routed through the override.
    reg.apply({ name: 'Autumn', budget: 5000 as unknown as string, startDate: '2026-08-01' });
    expect(adapter.store.name).toBe('Autumn');
    expect(adapter.store.budget).toBe('5000'); // coerced to string by the schema
    expect(adapter.store.duration).toEqual(['2026-08-01', null]); // bridged into slot 0

    // getState is bounded to the contract: declared fields + bridge reads.
    const state = reg.getState() as Record<string, unknown>;
    expect(state.name).toBe('Autumn');
    expect(state.startDate).toBe('2026-08-01');
  });

  it('getState projects to the contract — never leaks the raw form snapshot', () => {
    // The form holds undeclared fields and a non-plain `duration` tuple; neither
    // should escape past the schema boundary.
    const adapter = makeAdapter({ secret: 'do-not-send', duration: ['2026-08-01', '2026-08-31'] });
    adapter.set('name', 'X');
    const reg = zodFormRegistration({
      type: 'Campaign',
      id: 'new',
      scope: 'create',
      schema,
      adapter,
      overrides: {
        startDate: {
          read: (a) => (a.get('duration') as [string | null, string | null])?.[0] ?? null,
          write: () => {},
        },
      },
    });
    const state = reg.getState() as Record<string, unknown>;
    expect(Object.keys(state).sort()).toEqual(['budget', 'name', 'startDate']);
    expect(state).not.toHaveProperty('secret'); // undeclared field not leaked
    expect(state).not.toHaveProperty('duration'); // non-plain tuple not leaked
    expect(state.startDate).toBe('2026-08-01'); // the clean bridged value is sent instead
  });

  it('drops invalid/unknown fields without touching the form, and reports rejections', () => {
    const adapter = makeAdapter();
    const setSpy = vi.spyOn(adapter, 'set');
    const reg = zodFormRegistration({ type: 'Campaign', id: 'new', scope: 'create', schema, adapter });
    // `name` must be a string; a number fails safeParse → not written. An unknown key is
    // not written either — and it is REPORTED: a secret value the host keeps out of the
    // schema must not read as "applied" to the agent (it claimed exactly that).
    const result = reg.apply({ name: 123 as unknown as string, bogus: 'x' } as never);
    expect(setSpy).not.toHaveBeenCalled();
    // The rejections are surfaced, not silent.
    expect(result).toEqual({
      applied: [],
      rejected: [
        { field: 'name', reason: 'failed schema validation' },
        { field: 'bogus', reason: 'no such field in this form' },
      ],
    });
  });

  it('reports applied and rejected fields side by side', () => {
    const adapter = makeAdapter();
    const reg = zodFormRegistration({ type: 'Campaign', id: 'new', scope: 'create', schema, adapter });
    const result = reg.apply({ name: 'OK', budget: 12 as unknown as string, startDate: 42 as unknown as string });
    expect(result?.applied.sort()).toEqual(['budget', 'name']);
    expect(result?.rejected).toEqual([{ field: 'startDate', reason: 'failed schema validation' }]);
  });

  it('advertises only the fields the adapter serves and rejects writes to the rest', () => {
    const adapter = { ...makeAdapter(), has: (f: string) => f !== 'budget' };
    const setSpy = vi.spyOn(adapter, 'set');
    const reg = zodFormRegistration({ type: 'Campaign', id: '1', scope: 'update', schema, adapter });
    expect(reg.fields).toEqual(['name', 'startDate']);
    expect(reg.fieldSpecs?.map((s) => s.name)).toEqual(['name', 'startDate']);
    expect(Object.keys(reg.getState() as object)).toEqual(['name', 'startDate']);
    const result = reg.apply({ name: 'OK', budget: '5' });
    expect(result).toEqual({ applied: ['name'], rejected: [{ field: 'budget', reason: 'not editable in this form' }] });
    expect(setSpy).toHaveBeenCalledTimes(1);
  });

  it('asks a bridge, not the adapter, whether a bridged field is available', () => {
    const adapter = { ...makeAdapter(), has: () => false };
    const reg = zodFormRegistration({
      type: 'Campaign',
      id: '1',
      scope: 'update',
      schema,
      adapter,
      overrides: { startDate: { read: () => null, write: () => {}, available: () => true } },
    });
    expect(reg.fields).toEqual(['startDate']);
  });
});
