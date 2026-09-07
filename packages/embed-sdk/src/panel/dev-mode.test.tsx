// @vitest-environment happy-dom
/**
 * Where dev mode's on/off switch lives, and what it leaves behind.
 *
 * It used to sit in the panel header — which a host can remove
 * (`<AssistantPanel header={false}>`, exactly what the console's chat page
 * does), leaving dev mode with no control at all. It now rides the inspector's
 * own bar, the one piece of dev chrome that is on screen whenever dev mode is
 * AVAILABLE. Switched off, that bar is all that remains (the end-user view,
 * one click from coming back), and the choice is remembered per browser: the
 * chat page is a route, so plain state would put the chrome back on every
 * visit.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos } from '../core';
import { NannosProvider, type NannosHostAdapter } from '../react';
import { AssistantPanel } from './assistant-panel';

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

/** `header: false` mirrors the console's chat page — the shape that had no
 *  switch at all while it lived in the header. */
function mountPanel(opts: { devMode?: boolean; header?: false } = {}) {
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
  render(
    <NannosProvider core={core}>
      <AssistantPanel shadow={false} adapter={ADAPTER} devMode={opts.devMode} header={opts.header} />
    </NannosProvider>,
  );
}

const bar = () => document.querySelector('[data-slot="nannos-dev-inspector"]');
const devSwitch = () => screen.queryByRole('switch', { name: 'Toggle developer view' });

describe('dev mode', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    localStorage.clear();
  });
  afterEach(cleanup);

  it('shows nothing at all when the host did not enable it', () => {
    mountPanel({ devMode: false });
    expect(bar()).toBeNull();
    expect(devSwitch()).toBeNull();
  });

  it('a headerless surface still gets the inspector AND its switch', () => {
    // The console's chat page. Before the switch moved onto the bar, dev mode
    // here was on with no way to turn it off.
    mountPanel({ devMode: true, header: false });
    expect(document.querySelector('[data-slot="nannos-panel-header"]')).toBeNull();
    expect(bar()).toBeTruthy();
    expect(bar()!.contains(devSwitch())).toBe(true);
  });

  it('the header carries no dev switch any more — the bar is its one home', () => {
    mountPanel({ devMode: true });
    const header = document.querySelector('[data-slot="nannos-panel-header"]')!;
    expect(header).toBeTruthy();
    expect(header.querySelector('[data-slot="nannos-dev-switch"]')).toBeNull();
  });

  it('switching off leaves the bar and nothing else', () => {
    mountPanel({ devMode: true, header: false });
    // Open it first: the body must go even when it was expanded.
    fireEvent.click(document.querySelector('[data-slot="nannos-dev-inspector"] summary')!);

    fireEvent.click(devSwitch()!);

    // The bar stays — it carries the only way back.
    expect(bar()).toBeTruthy();
    expect(bar()!.getAttribute('data-inactive')).toBe('true');
    expect(devSwitch()).toBeTruthy();
    // …and everything the inspector rendered is gone.
    expect(bar()!.querySelector('summary')).toBeNull();
    expect(document.querySelector('[data-slot="nannos-dev-trace"]')).toBeNull();
  });

  it('clicking the switch does not also toggle the bar open', () => {
    // It sits inside a <summary>, whose default action would answer the click.
    mountPanel({ devMode: true, header: false });
    const details = document.querySelector('[data-slot="nannos-dev-inspector"]') as HTMLDetailsElement;
    expect(details.open).toBe(false);

    fireEvent.click(devSwitch()!);
    fireEvent.click(devSwitch()!); // back on — the bar is a <details> again

    const reopened = document.querySelector('[data-slot="nannos-dev-inspector"]') as HTMLDetailsElement;
    expect(reopened.open).toBe(false);
  });

  it('remembers the choice across mounts — the chat page is a route', () => {
    mountPanel({ devMode: true, header: false });
    fireEvent.click(devSwitch()!);
    expect(bar()!.getAttribute('data-inactive')).toBe('true');

    cleanup();
    mountPanel({ devMode: true, header: false });
    expect(bar()!.getAttribute('data-inactive')).toBe('true');
  });
});
