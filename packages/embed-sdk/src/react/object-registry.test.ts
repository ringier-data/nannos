/**
 * Generic derive-function contract for the object-type registry — one fixture
 * type per idShape, plus label overrides and scoped highlight resolution.
 * (The cockpit keeps its own registry-completeness + identity test against its
 * real types; this suite pins the SDK-side semantics those rely on.)
 */
import { describe, expect, it } from 'vitest';
import {
  deriveManifestLabel,
  deriveObjectId,
  deriveScope,
  isExistingId,
  resolveHighlightLabel,
  type ObjectTypeRegistry,
  type RouteId,
} from './object-registry';

const schema = { shape: {} };

const registry: ObjectTypeRegistry = {
  Invoice: { schema, singular: 'Invoice', idShape: 'simple-numeric', highlightLabels: { dueDate: 'Due date' } },
  Topic: { schema, singular: 'Topic', idShape: 'simple-string' },
  Plan: { schema, singular: 'Plan', idShape: 'simple-sentinel', createSentinel: 'create' },
  LineItem: {
    schema,
    singular: 'Line item',
    idShape: 'nested',
    parentSingular: 'invoice',
    highlightLabels: { amount: 'Amount' },
  },
  Briefing: {
    schema,
    singular: 'Briefing',
    idShape: 'simple-sentinel',
    label: ({ id, isExisting }) => (isExisting ? `Plan ${id} briefing` : 'New plan briefing'),
  },
};

type Case = {
  type: keyof typeof registry & string;
  id: RouteId;
  parentId?: RouteId;
  expectedId: string;
  expectedScope: string;
  expectedLabel: string;
};

const cases: Case[] = [
  // simple-numeric — the create route resolves the id to NaN/undefined.
  { type: 'Invoice', id: 42, expectedId: '42', expectedScope: 'update', expectedLabel: 'Invoice 42' },
  { type: 'Invoice', id: undefined, expectedId: 'new', expectedScope: 'create', expectedLabel: 'New invoice' },
  { type: 'Invoice', id: NaN, expectedId: 'new', expectedScope: 'create', expectedLabel: 'New invoice' },
  // `Number(null)` is 0, not NaN — null must still mean "create".
  { type: 'Invoice', id: null, expectedId: 'new', expectedScope: 'create', expectedLabel: 'New invoice' },

  // simple-string — any non-empty string is an existing id.
  { type: 'Topic', id: 'abc', expectedId: 'abc', expectedScope: 'update', expectedLabel: 'Topic abc' },
  { type: 'Topic', id: undefined, expectedId: 'new', expectedScope: 'create', expectedLabel: 'New topic' },

  // simple-sentinel — the create route reuses the details page with a literal.
  { type: 'Plan', id: '7', expectedId: '7', expectedScope: 'update', expectedLabel: 'Plan 7' },
  { type: 'Plan', id: 'create', expectedId: 'new', expectedScope: 'create', expectedLabel: 'New plan' },

  // custom label fn wins over the derived shape.
  { type: 'Briefing', id: '7', expectedId: '7', expectedScope: 'update', expectedLabel: 'Plan 7 briefing' },
  { type: 'Briefing', id: 'create', expectedId: 'new', expectedScope: 'create', expectedLabel: 'New plan briefing' },

  // nested — id is `parent/child`, label names the parent.
  { type: 'LineItem', id: 3, parentId: 12, expectedId: '12/3', expectedScope: 'update', expectedLabel: 'Line item 3 (invoice 12)' },
  { type: 'LineItem', id: undefined, parentId: 12, expectedId: '12/new', expectedScope: 'create', expectedLabel: 'New line item (invoice 12)' },
  // A child rendered before its parent id resolves — degrade, don't throw.
  { type: 'LineItem', id: undefined, parentId: undefined, expectedId: 'unknown/new', expectedScope: 'create', expectedLabel: 'New line item (invoice undefined)' },
];

describe('object-registry derive functions', () => {
  it.each(cases)('$type id=$id parentId=$parentId', ({ type, id, parentId, expectedId, expectedScope, expectedLabel }) => {
    const definition = registry[type];
    expect(deriveObjectId(definition, id, parentId)).toBe(expectedId);
    expect(deriveScope(isExistingId(definition, id))).toBe(expectedScope);
    expect(deriveManifestLabel(definition, id, parentId)).toBe(expectedLabel);
  });
});

describe('resolveHighlightLabel', () => {
  it("answers only for the named type — never leaks another form's labels", () => {
    expect(resolveHighlightLabel(registry, 'Invoice', 'dueDate')).toBe('Due date');
    expect(resolveHighlightLabel(registry, 'Invoice', 'amount')).toBeUndefined();
    expect(resolveHighlightLabel(registry, 'Topic', 'dueDate')).toBeUndefined();
  });

  it('scans everything only for an UNKNOWN type', () => {
    expect(resolveHighlightLabel(registry, undefined, 'amount')).toBe('Amount');
    expect(resolveHighlightLabel(registry, undefined, 'nope')).toBeUndefined();
  });
});
