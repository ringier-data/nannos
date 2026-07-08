import { describe, expect, it, vi } from 'vitest';
import { ObjectRegistry } from './registry';

describe('ObjectRegistry', () => {
  const base = () => ({
    type: 'Campaign',
    id: '123',
    scope: 'update' as const,
    schema: { fields: ['name'] },
    getState: () => ({ name: 'Spring' }),
    apply: vi.fn(),
    label: 'Spring campaign',
  });

  it('manifest exposes only the compact index (no schema, no state)', () => {
    const r = new ObjectRegistry();
    r.register(base());
    expect(r.manifest()).toEqual([
      { type: 'Campaign', id: '123', scope: 'update', label: 'Spring campaign' },
    ]);
  });

  it('detail pulls schema + current state on demand', () => {
    const r = new ObjectRegistry();
    r.register(base());
    expect(r.detail('Campaign', '123')).toEqual({
      schema: { fields: ['name'] },
      state: { name: 'Spring' },
    });
    expect(r.detail('Campaign', 'nope')).toBeUndefined();
  });

  it('dispose removes the object and unknown lookups return undefined', () => {
    const r = new ObjectRegistry();
    const h = r.register(base());
    expect(r.get('Campaign', '123')).toBeDefined();
    h.dispose();
    expect(r.get('Campaign', '123')).toBeUndefined();
    expect(r.manifest()).toEqual([]);
  });

  it('notifies onChange listeners on register and dispose', () => {
    const r = new ObjectRegistry();
    const fn = vi.fn();
    r.onChange(fn);
    const h = r.register(base());
    h.dispose();
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
