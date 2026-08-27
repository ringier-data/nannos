// @vitest-environment happy-dom
/**
 * Where the apply-mode control lives, and that the popover explains itself.
 *
 * It belongs in the composer, immediately left of send: the user is there when
 * they ask for a form fill, so "will it ask me first?" is answered at the
 * moment the question matters. Clicking it must show BOTH modes with what each
 * one does — a label alone ("Manual") is not an explanation, and a mode nobody
 * understands is a mode nobody changes. A host that fixed the mode must see no
 * control at all: a locked setting that looks adjustable is worse than none —
 * and neither must a host that registered no object to apply INTO, where the
 * setting governs something that cannot happen.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos } from '../core';
import { NannosProvider, type NannosHostAdapter } from '../react';
import { AssistantPanel } from './assistant-panel';
import type { ApplyMode } from './apply-mode';

class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

class FakeSocket {
  connected = false;
  on() {
    return this;
  }
  emit() {}
  connect() {}
  disconnect() {}
}

const ADAPTER: NannosHostAdapter = {
  defaults: { agentUrl: 'http://agent', model: 'm1' },
  api: { getUserSettings: async () => null },
};

/** Every test but the "nothing to apply into" one needs a registered target:
 *  with an empty registry the switch hides itself on purpose. */
function mountPanel(
  applyMode?: ApplyMode,
  opts: { shadow?: boolean; register?: boolean } = {},
) {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify({ conversations: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    ),
  );
  const core = createNannos({}, () => new FakeSocket() as unknown as Socket);
  if (opts.register !== false) {
    core.register({ type: 'campaign', id: '1', scope: 'update', getState: () => ({}), apply: () => {} });
  }
  render(
    <NannosProvider core={core}>
      <AssistantPanel shadow={opts.shadow ?? false} adapter={ADAPTER} applyMode={applyMode} />
    </NannosProvider>,
  );
}

const control = () => document.querySelector('[data-slot="nannos-apply-mode"]');
const send = () => document.querySelector('[data-slot="nannos-composer-send"]');
const menu = () => document.querySelector('[data-slot="nannos-apply-mode-menu"]');
const options = () =>
  Array.from(document.querySelectorAll('[data-slot="nannos-apply-mode-option"]'));
const option = (mode: ApplyMode) =>
  document.querySelector(`[data-slot="nannos-apply-mode-option"][data-mode="${mode}"]`)!;

describe('apply-mode switch', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    localStorage.clear();
  });
  afterEach(cleanup);

  it('sits in the composer, immediately before send', () => {
    mountPanel();
    const button = control();
    expect(button).toBeTruthy();
    // Same row as send, and the element right after it IS send.
    expect(button!.parentElement).toBe(send()!.parentElement);
    expect(button!.nextElementSibling).toBe(send());
  });

  it('reads Manual by default and shows no menu until asked', () => {
    mountPanel();
    expect(control()!.getAttribute('data-mode')).toBe('manual');
    expect(control()!.textContent).toContain('Manual');
    expect(menu()).toBeNull();
  });

  it('a click opens a menu naming both modes AND describing each', () => {
    mountPanel();
    fireEvent.click(control()!);

    expect(menu()).toBeTruthy();
    expect(options()).toHaveLength(2);

    // The explanation is the point of the popover, so every row must carry a
    // description under its name. Asserted structurally, NOT by matching the
    // copy: the wording is edited freely (and per locale) and a test that
    // pins sentences only ever fails for the wrong reason.
    for (const mode of ['manual', 'allow-edits'] as const) {
      const [name, description] = Array.from(option(mode).querySelectorAll('span > span'));
      expect(name.textContent!.trim().length).toBeGreaterThan(0);
      expect(description.textContent!.trim().length).toBeGreaterThan(0);
      expect(description.textContent).not.toBe(name.textContent);
    }
    // The names are the two modes, in the cautious-first order.
    expect(options().map((o) => o.getAttribute('data-mode'))).toEqual(['manual', 'allow-edits']);
  });

  it('marks the mode that is on, and picking the other switches and closes', () => {
    mountPanel();
    fireEvent.click(control()!);
    expect(option('manual').getAttribute('aria-checked')).toBe('true');
    expect(option('allow-edits').getAttribute('aria-checked')).toBe('false');

    fireEvent.click(option('allow-edits'));

    expect(menu()).toBeNull();
    expect(control()!.getAttribute('data-mode')).toBe('allow-edits');
    expect(control()!.textContent).toContain('Allow edits');

    // Reopening shows the check on the new mode.
    fireEvent.click(control()!);
    expect(option('allow-edits').getAttribute('aria-checked')).toBe('true');
    expect(option('manual').getAttribute('aria-checked')).toBe('false');
  });

  it('opens above the composer, so it never covers the input', () => {
    mountPanel();
    fireEvent.click(control()!);
    expect(menu()!.getAttribute('data-side')).toBe('top');
  });

  it('the mode that is on reads bold — the check is not the only signal', () => {
    mountPanel();
    fireEvent.click(control()!);
    const nameOf = (mode: ApplyMode) => option(mode).querySelector('span > span')!;
    expect(nameOf('manual').className).toContain('font-bold');
    expect(nameOf('allow-edits').className).not.toContain('font-bold');

    fireEvent.click(option('allow-edits'));
    fireEvent.click(control()!);
    expect(nameOf('allow-edits').className).toContain('font-bold');
    expect(nameOf('manual').className).not.toContain('font-bold');
  });

  it('renders nothing when no client object is registered', () => {
    // Nothing to apply into → the mode governs something that cannot happen.
    mountPanel(undefined, { register: false });
    expect(control()).toBeNull();
    expect(send()).toBeTruthy();
  });

  it('renders nothing when the host fixed the mode', () => {
    mountPanel('allow-edits');
    expect(control()).toBeNull();
    // The composer is still there — only the control is gone.
    expect(send()).toBeTruthy();
  });

  it('the header carries no mode control', () => {
    // It used to live there; the composer is the one home for it.
    mountPanel();
    const header = document.querySelector('[data-slot="nannos-panel-header"]');
    expect(header).toBeTruthy();
    expect(header!.querySelector('[data-slot="nannos-apply-mode"]')).toBeNull();
  });
});

describe('apply-mode switch: the shadow root it actually runs in', () => {
  // Why these exist: every test above mounts with `shadow={false}`, so none of
  // them can tell an in-tree menu from a portalled one — and a portalled Radix
  // popover does not open inside the panel's Shadow DOM at all (its dismissable
  // layer hit-tests against `document`, where shadow events retarget to the
  // host, so it dismisses itself as it opens). That regression is invisible to
  // a light-DOM render, which is exactly how it shipped twice.
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    localStorage.clear();
  });
  afterEach(cleanup);

  it('the menu lives INSIDE the composer row — never portalled away', () => {
    mountPanel();
    fireEvent.click(control()!);
    // A portal would hang it off the shadow container (or document.body)
    // instead, which is the shape that silently fails.
    expect(menu()!.closest('[data-slot="nannos-composer-actions"]')).toBeTruthy();
    expect(menu()!.parentElement).toBe(control()!.parentElement);
  });

  it('opens when the panel is rendered in a real shadow root', () => {
    mountPanel(undefined, { shadow: true });
    const host = document.querySelector('[data-nannos-ignore]') as HTMLElement;
    const root = host.shadowRoot;
    expect(root).toBeTruthy();

    const trigger = root!.querySelector('[data-slot="nannos-apply-mode"]') as HTMLElement;
    expect(trigger).toBeTruthy();
    fireEvent.click(trigger);

    // Inside the shadow root, where the panel's styles reach it.
    const shadowMenu = root!.querySelector('[data-slot="nannos-apply-mode-menu"]');
    expect(shadowMenu).toBeTruthy();
    expect(shadowMenu!.querySelectorAll('[data-slot="nannos-apply-mode-option"]')).toHaveLength(2);
  });
});
