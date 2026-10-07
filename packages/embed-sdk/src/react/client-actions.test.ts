// @vitest-environment happy-dom
/**
 * Generic client-action handlers: same-origin navigation guard and the
 * design-system-agnostic field lookup behind `highlight`.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createClientActionHandlers, findFieldElement, isAssistantElement, resolveSameOriginPath } from './client-actions';
import { ChangeStore } from '../core/change-store';

afterEach(() => {
  document.body.innerHTML = '';
  vi.useRealTimers();
});

describe('resolveSameOriginPath', () => {
  it('keeps same-origin paths with search and hash', () => {
    expect(resolveSameOriginPath('/app/x?a=1#b')).toBe('/app/x?a=1#b');
    expect(resolveSameOriginPath(`${window.location.origin}/app`)).toBe('/app');
  });

  it('refuses other origins, protocol-relative included', () => {
    expect(resolveSameOriginPath('https://evil.example/app')).toBeNull();
    expect(resolveSameOriginPath('//evil.example/app')).toBeNull();
  });
});

describe('navigate', () => {
  it('routes same-origin targets through the host and drops the rest', () => {
    const navigate = vi.fn();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const handlers = createClientActionHandlers({ navigate });
    handlers.navigate('/app/scheduler');
    handlers.navigate('https://evil.example/');
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith('/app/scheduler');
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe('findFieldElement', () => {
  it('prefers data-nannos-field over name', () => {
    document.body.innerHTML = `
      <input name="title" id="by-name" />
      <div data-nannos-field="title" id="marked"></div>`;
    expect(findFieldElement(undefined, 'title')?.id).toBe('marked');
  });

  it('falls back to [name]', () => {
    document.body.innerHTML = `<input name="title" id="by-name" />`;
    expect(findFieldElement(undefined, 'title')?.id).toBe('by-name');
  });

  it('resolves a declared label through its for= target', () => {
    document.body.innerHTML = `<div><label for="tz">Time zone</label><button id="tz"></button></div>`;
    const el = findFieldElement({ type: 'Settings', id: 'me' }, 'timezone', () => 'time zone');
    expect(el?.id).toBe('tz');
  });

  it('scopes the lookup to the marked object container', () => {
    document.body.innerHTML = `
      <form data-nannos-object="A:1"><input name="name" id="a" /></form>
      <form data-nannos-object="B:2"><input name="name" id="b" /></form>`;
    expect(findFieldElement({ type: 'B', id: '2' }, 'name')?.id).toBe('b');
  });

  it('escapes agent-supplied field names', () => {
    document.body.innerHTML = `<input name="x" />`;
    expect(findFieldElement(undefined, '"] , body [x="')).toBeNull();
  });
});

describe('highlight', () => {
  it('outlines the field and restores the original shadow', () => {
    vi.useFakeTimers();
    document.body.innerHTML = `<input name="title" style="box-shadow: 1px 1px red" />`;
    const el = document.querySelector('input')!;
    el.scrollIntoView = vi.fn();
    const { highlight } = createClientActionHandlers({ highlightColor: 'blue' });
    highlight({ type: 'T', id: '1' }, 'title');
    highlight({ type: 'T', id: '1' }, 'title');
    expect(el.style.boxShadow).toContain('blue');
    vi.advanceTimersByTime(2000);
    expect(el.style.boxShadow).toBe('1px 1px red');
  });
});

describe('findFieldElement without a marked container', () => {
  it('prefers the open dialog over the page behind it', () => {
    document.body.innerHTML = `
      <label for="page-name">Name</label><input id="page-name" />
      <div role="dialog"><label for="dialog-name">Name</label><input id="dialog-name" /></div>`;
    expect(findFieldElement({ type: 'Catalog', id: 'new' }, 'name', () => 'Name')?.id).toBe('dialog-name');
  });

  it('falls back to the page when the dialog lacks the field', () => {
    document.body.innerHTML = `
      <input name="title" id="page-title" />
      <div role="dialog"><input name="other" /></div>`;
    expect(findFieldElement(undefined, 'title')?.id).toBe('page-title');
  });
});

describe('isAssistantElement', () => {
  it('matches the marked container and its descendants only', () => {
    document.body.innerHTML = `<div data-nannos-assistant><button id="in"></button></div><button id="out"></button>`;
    expect(isAssistantElement(document.getElementById('in'))).toBe(true);
    expect(isAssistantElement(document.getElementById('out'))).toBe(false);
    expect(isAssistantElement(null)).toBe(false);
  });
});

describe('changed-field marks', () => {
  function setup() {
    document.head.innerHTML = '';
    document.body.innerHTML = `<div><input name="language" value="English" /></div><div><input name="timezone" value="" /></div>`;
    const [language, timezone] = [...document.querySelectorAll('input')];
    language.scrollIntoView = vi.fn();
    const store = new ChangeStore();
    const target = { type: 'Settings', id: 'me' };
    const writes: Record<string, unknown>[] = [];
    const handlers = createClientActionHandlers();
    const captured = handlers.beforeApply(target, ['language', 'timezone']);
    const changes = store.record(target, { language: 'en', timezone: '' }, ['language', 'timezone'], async (v) => {
      writes.push(v);
      return Object.keys(v);
    });
    handlers.markChanged(target, changes, captured);
    return { language, timezone, store, target, writes, handlers };
  }

  it('shimmers once, rings, and notes what the field held, as the user saw it', () => {
    const { language, timezone } = setup();
    expect(language.getAttribute('data-nannos-changed')).toBe('fresh');
    expect(document.getElementById('nannos-change-marks')).not.toBeNull();
    const note = language.nextElementSibling as HTMLElement;
    expect(note.hasAttribute('data-nannos-change-note')).toBe(true);
    expect(note.textContent).toContain('Set by Nannos');
    expect(note.querySelector('s')?.textContent).toBe('English'); // the label, not the code 'en'
    expect((timezone.nextElementSibling as HTMLElement).querySelector('em')?.textContent).toBe('empty');
    expect(language.scrollIntoView).toHaveBeenCalledOnce();
  });

  it('Undo writes the old value back and removes the mark', async () => {
    const { language, store, target, writes } = setup();
    (language.nextElementSibling as HTMLElement).querySelector('button')!.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(writes).toEqual([{ language: 'en' }]);
    expect(language.hasAttribute('data-nannos-changed')).toBe(false);
    expect(language.nextElementSibling).toBeNull();
    expect(store.get(target).map((c) => c.field)).toEqual(['timezone']);
  });

  it('the user editing a field ends its mark; a save ends all', () => {
    const { language, timezone, store, target, handlers } = setup();
    language.dispatchEvent(new Event('input', { bubbles: true }));
    expect(language.hasAttribute('data-nannos-changed')).toBe(false);
    expect(store.get(target).map((c) => c.field)).toEqual(['timezone']);
    store.clear(target);
    handlers.clearChanged(target);
    expect(timezone.hasAttribute('data-nannos-changed')).toBe(false);
    expect(document.querySelectorAll('[data-nannos-change-note]')).toHaveLength(0);
  });
});

describe('what the user saw', () => {
  it('reads a labelled wrapper through its control, not its label', () => {
    document.body.innerHTML = `<div data-nannos-field="schedule_kind"><label>Schedule</label><button role="combobox">Cron expression</button></div>`;
    const { beforeApply } = createClientActionHandlers();
    expect(beforeApply({ type: 'Job', id: 'new' }, ['schedule_kind'])).toEqual({ schedule_kind: 'Cron expression' });
  });
});

describe('marking the control, not its wrapper', () => {
  it('a labelled wrapper with one control marks the control', () => {
    document.head.innerHTML = '';
    document.body.innerHTML = `<div data-nannos-field="schedule_kind" id="wrap"><label>Schedule</label><button role="combobox" id="ctl">Run once</button></div>`;
    const store = new ChangeStore();
    const target = { type: 'Job', id: 'new' };
    const handlers = createClientActionHandlers();
    const ctl = document.getElementById('ctl')!;
    ctl.scrollIntoView = vi.fn();
    const captured = handlers.beforeApply(target, ['schedule_kind']);
    handlers.markChanged(target, store.record(target, { schedule_kind: 'cron' }, ['schedule_kind'], async (v) => Object.keys(v)), captured);
    expect(ctl.hasAttribute('data-nannos-changed')).toBe(true);
    expect(document.getElementById('wrap')!.hasAttribute('data-nannos-changed')).toBe(false);
    expect(ctl.nextElementSibling?.hasAttribute('data-nannos-change-note')).toBe(true);
    expect(ctl.style.getPropertyValue('--nannos-fill')).not.toBe('');
  });

  it('a field on a tinted card is filled with every translucent layer down to an opaque one', () => {
    document.head.innerHTML = '';
    document.body.innerHTML = `<div id="page" style="background-color: rgb(255, 255, 255)"><div id="card" style="background-color: rgba(255, 200, 0, 0.1)"><button role="combobox" id="ctl" data-nannos-field="model">Standard tier</button></div></div>`;
    const store = new ChangeStore();
    const target = { type: 'SubAgent', id: '1' };
    const handlers = createClientActionHandlers();
    const ctl = document.getElementById('ctl')!;
    ctl.scrollIntoView = vi.fn();
    handlers.markChanged(target, store.record(target, { model: 'tier:premium' }, ['model'], async (v) => Object.keys(v)), {});
    // Only the card's tint would leave the edge gradient showing through the whole
    // field; the opaque page colour underneath has to be part of the fill.
    const fill = ctl.style.getPropertyValue('--nannos-fill');
    expect(fill.split('padding-box').length - 1).toBe(2);
    expect(fill).toContain('255, 200, 0');
    expect(fill).toContain('rgb(255, 255, 255)');
    expect(ctl.style.getPropertyValue('--nannos-rest-size').split(',')).toHaveLength(3);
  });

  it('a theme switch repaints the fill of fields already marked', async () => {
    // Marked in light mode, the fill stayed white in dark mode and hid the light text.
    document.head.innerHTML = '';
    document.body.innerHTML = `<div id="page" style="background-color: rgb(255, 255, 255)"><input id="ctl" name="name" /></div>`;
    const store = new ChangeStore();
    const target = { type: 'Job', id: 'new' };
    const handlers = createClientActionHandlers();
    const ctl = document.getElementById('ctl')!;
    ctl.scrollIntoView = vi.fn();
    handlers.markChanged(target, store.record(target, { name: 'x' }, ['name'], async (v) => Object.keys(v)), {});
    expect(ctl.style.getPropertyValue('--nannos-fill')).toContain('rgb(255, 255, 255)');

    document.getElementById('page')!.style.backgroundColor = 'rgb(10, 10, 20)';
    document.documentElement.classList.add('dark');
    await new Promise((r) => setTimeout(r, 0));
    expect(ctl.style.getPropertyValue('--nannos-fill')).toContain('rgb(10, 10, 20)');
    expect(ctl.getAttribute('data-nannos-changed')).not.toBeNull();
    document.documentElement.classList.remove('dark');
  });

  it('a width variable written on <html> (the dock resizing) does not repaint', async () => {
    document.head.innerHTML = '';
    document.body.innerHTML = `<div id="page" style="background-color: rgb(255, 255, 255)"><input id="ctl" name="name" /></div>`;
    const store = new ChangeStore();
    const target = { type: 'Job', id: 'new' };
    const handlers = createClientActionHandlers();
    const ctl = document.getElementById('ctl')!;
    ctl.scrollIntoView = vi.fn();
    handlers.markChanged(target, store.record(target, { name: 'x' }, ['name'], async (v) => Object.keys(v)), {});

    document.getElementById('page')!.style.backgroundColor = 'rgb(10, 10, 20)';
    document.documentElement.style.setProperty('--nannos-panel-width', '420px');
    await new Promise((r) => setTimeout(r, 0));
    expect(ctl.style.getPropertyValue('--nannos-fill')).toContain('rgb(255, 255, 255)');
    document.documentElement.style.removeProperty('--nannos-panel-width');
  });

  it('a wrapper with several controls stays the field', () => {
    document.body.innerHTML = `<div data-nannos-field="mcp_tools" id="wrap"><input name="search" /><input type="checkbox" /><input type="checkbox" /></div>`;
    const store = new ChangeStore();
    const target = { type: 'SubAgent', id: '1' };
    const handlers = createClientActionHandlers();
    const wrap = document.getElementById('wrap')!;
    wrap.scrollIntoView = vi.fn();
    handlers.markChanged(target, store.record(target, { mcp_tools: [] }, ['mcp_tools'], async (v) => Object.keys(v)), {});
    expect(wrap.hasAttribute('data-nannos-changed')).toBe(true);
  });
});
