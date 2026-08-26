/**
 * The page-context fold + second-line-of-defence sanitizer (everything in the
 * snapshot reaches a model). Behaviors ported from Gatana's page-context.
 */
import { describe, expect, it } from 'vitest';
import { mergePageContexts, sanitizePageContext } from './page-context';

describe('mergePageContexts', () => {
  it('later layer wins field-by-field; view merges key by key', () => {
    const merged = mergePageContexts([
      { key: '/campaigns/7', title: 'Campaign 7', view: { tab: 'overview', status: 'active' } },
      { entity: { type: 'Campaign', id: '7', name: 'Summer' }, view: { tab: 'targetings' } },
    ]);
    expect(merged).toEqual({
      key: '/campaigns/7', // only the base declared one
      title: 'Campaign 7',
      entity: { type: 'Campaign', id: '7', name: 'Summer' },
      // The tab's view key overrode the page's; the page's other key survived.
      view: { tab: 'targetings', status: 'active' },
    });
  });

  it('an empty later layer does not erase earlier fields', () => {
    const merged = mergePageContexts([{ key: '/a', title: 'A' }, {}]);
    expect(merged).toEqual({ key: '/a', title: 'A' });
  });
});

describe('sanitizePageContext', () => {
  it('requires a key — a snapshot with no page identity is not published', () => {
    expect(sanitizePageContext({})).toBeNull();
    expect(sanitizePageContext({ title: 'orphan' })).toBeNull();
    expect(sanitizePageContext({ key: '   ' })).toBeNull();
  });

  it('clamps key/title/entity to their caps', () => {
    const out = sanitizePageContext({
      key: `/x/${'k'.repeat(600)}`,
      title: 't'.repeat(200),
      entity: { type: 'Campaign', id: 'i'.repeat(300), name: 'n'.repeat(200) },
    })!;
    expect(out.key!.length).toBeLessThanOrEqual(500);
    expect(out.title!.length).toBeLessThanOrEqual(160);
    expect(out.entity!.id.length).toBeLessThanOrEqual(200);
    expect(out.entity!.name!.length).toBeLessThanOrEqual(160);
  });

  it('caps breadcrumbs and visible by count and entry length', () => {
    const out = sanitizePageContext({
      key: '/x',
      breadcrumbs: Array.from({ length: 12 }, (_, i) => `crumb ${i} ${'b'.repeat(150)}`),
      visible: Array.from({ length: 30 }, (_, i) => `row ${i}`),
    })!;
    expect(out.breadcrumbs).toHaveLength(8);
    expect(out.breadcrumbs![0].length).toBeLessThanOrEqual(120);
    expect(out.visible).toHaveLength(25);
  });

  it('drops secret-looking view keys, keeps state that merely DESCRIBES one', () => {
    const out = sanitizePageContext({
      key: '/settings',
      view: {
        apiKey: 'sk-oops',
        access_token: 'oops',
        bearerValue: 'oops',
        hasCredentials: true, // describes a secret — exactly what the agent needs
        authMethod: 'oauth',
      },
    })!;
    expect(out.view).toEqual({ hasCredentials: true, authMethod: 'oauth' });
  });

  it('keeps scalars only in view, dropping empties; over the key cap the LAST (most specific) keys survive', () => {
    const wide: Record<string, string | number | boolean | null | undefined> = {};
    for (let i = 0; i < 20; i++) wide[`k${i}`] = i;
    const out = sanitizePageContext({
      key: '/x',
      // The window is POSITIONAL (last 16 entries), then empties inside it are
      // dropped — a merged payload puts the most specific (tab/dialog) keys
      // last, which is what the window is protecting.
      view: { empty: '  ', gone: null, missing: undefined, ...wide },
    })!;
    expect(Object.keys(out.view!)).toEqual(Array.from({ length: 16 }, (_, i) => `k${i + 4}`));
    expect(out.view!.k19).toBe(19);
    expect('empty' in out.view!).toBe(false);
  });

  it('sheds visible first when that alone brings the payload under the ceiling', () => {
    const out = sanitizePageContext({
      key: '/big',
      title: 'Big page',
      // ~1.4k of view + ~2.8k of visible: dropping visible is enough.
      view: Object.fromEntries(Array.from({ length: 10 }, (_, i) => [`key${i}`, 'v'.repeat(130)])),
      visible: Array.from({ length: 25 }, () => 'x'.repeat(110)),
    })!;
    expect(JSON.stringify(out).length).toBeLessThanOrEqual(2000);
    expect(out.visible).toBeUndefined();
    expect(out.view).toBeDefined(); // the more specific field survived
  });

  it('sheds view too when visible alone was not enough — what is left still identifies the page', () => {
    const out = sanitizePageContext({
      key: '/big',
      title: 'Big page',
      view: Object.fromEntries(Array.from({ length: 16 }, (_, i) => [`key${i}`, 'v'.repeat(190)])),
      visible: Array.from({ length: 25 }, () => 'x'.repeat(110)),
    })!;
    expect(JSON.stringify(out).length).toBeLessThanOrEqual(2000);
    expect(out.visible).toBeUndefined();
    expect(out.view).toBeUndefined();
    expect(out.key).toBe('/big');
    expect(out.title).toBe('Big page');
  });
});
