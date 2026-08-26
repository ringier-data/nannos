// @vitest-environment happy-dom
/**
 * Provider state machine: persistence contract (pin/width/open restore),
 * clamping, draft-vs-send seeding, gesture login gating, CSS-variable
 * publication, string-table merge, StrictMode remount, and the no-provider
 * UNAVAILABLE fallback.
 */
import { act, render, renderHook } from '@testing-library/react';
import { StrictMode, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos, type NannosAuth } from '../core';
import {
  NANNOS_PANEL_WIDTH_VAR,
  NannosProvider,
  clampPanelWidth,
  useAssistant,
  type NannosProviderProps,
} from './provider';
import { useStrings, format } from './i18n';
import { useNannosPageContext } from './use-page-context';
import { useNannosPageReader } from './use-page-reader';

// Minimal fake socket so createNannos never touches the network.
class FakeSocket {
  connected = false;
  handlers = new Map<string, (data: unknown) => void>();
  on(event: string, cb: (data: unknown) => void) {
    this.handlers.set(event, cb);
    return this;
  }
  emit() {}
  connect() {
    this.connected = true;
  }
  disconnect() {
    this.connected = false;
  }
}

const ioFactory = () => new FakeSocket() as unknown as Socket;
const makeCore = () => createNannos({}, ioFactory);

function wrapper(props: Partial<NannosProviderProps> = {}) {
  return ({ children }: { children: ReactNode }) => (
    <NannosProvider core={makeCore()} {...props}>
      {children}
    </NannosProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});
afterEach(() => {
  document.documentElement.style.removeProperty(NANNOS_PANEL_WIDTH_VAR);
});

describe('clampPanelWidth', () => {
  it('clamps into [min, min(960, viewport-360)] and rounds', () => {
    // happy-dom default innerWidth is 1024 → widest = min(960, 664) = 664.
    expect(clampPanelWidth(100)).toBe(420);
    expect(clampPanelWidth(500.6)).toBe(501);
    expect(clampPanelWidth(5000)).toBe(Math.min(960, window.innerWidth - 360));
  });
  it('never lets max fall below min (tiny viewports stay draggable)', () => {
    expect(clampPanelWidth(9999, { min: 420, max: () => 100 })).toBe(420);
  });
});

describe('open/pin/width state machine + persistence', () => {
  it('starts pinned & closed by default; open/close round-trips; close drops the seed', () => {
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper() });
    expect(result.current.isAvailable).toBe(true);
    expect(result.current.isPinned).toBe(true);
    expect(result.current.isOpen).toBe(false);

    act(() => result.current.open('draft me', { displayText: 'Chip' }));
    expect(result.current.isOpen).toBe(true);
    expect(result.current.seededPrompt).toMatchObject({
      text: 'draft me',
      displayText: 'Chip',
      sendOnOpen: false, // DRAFT by default
    });

    act(() => result.current.close());
    expect(result.current.isOpen).toBe(false);
    expect(result.current.seededPrompt).toBeNull();
  });

  it('sendOnOpen and contextKey ride the seed explicitly', () => {
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper() });
    act(() => result.current.open('do it', { sendOnOpen: true, contextKey: 'campaign:7' }));
    expect(result.current.seededPrompt).toMatchObject({
      text: 'do it',
      sendOnOpen: true,
      contextKey: 'campaign:7',
    });
  });

  it('page context: reactive, value-equal updates dropped, and open() scopes seeds by its key', () => {
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper() });
    expect(result.current.pageContext).toBeNull();

    act(() => result.current.setPageContext({ key: '/campaigns/7', title: 'Campaign 7' }));
    expect(result.current.pageContext).toEqual({ key: '/campaigns/7', title: 'Campaign 7' });

    // A re-publish of the same VALUE (fresh object) must not change identity —
    // hosts publish from router effects on every render.
    const before = result.current.pageContext;
    act(() => result.current.setPageContext({ key: '/campaigns/7', title: 'Campaign 7' }));
    expect(result.current.pageContext).toBe(before);

    // A seeded prompt defaults its conversation scope to the live page key…
    act(() => result.current.open('about this page'));
    expect(result.current.seededPrompt).toMatchObject({ contextKey: '/campaigns/7' });

    // …but an explicit key still wins.
    act(() => result.current.open('elsewhere', { contextKey: 'campaign:9' }));
    expect(result.current.seededPrompt).toMatchObject({ contextKey: 'campaign:9' });

    act(() => result.current.setPageContext(null));
    expect(result.current.pageContext).toBeNull();
  });

  it('page context layers: base + page + tab merge (later wins, view merges), sanitized, disposable', () => {
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper() });

    // The router bridge holds the base…
    act(() => result.current.setPageContext({ key: '/campaigns/7', title: 'Campaign 7' }));
    // …a details page layers its entity (plus a secret that must never leave)…
    let page: ReturnType<typeof result.current.registerPageContextLayer>;
    act(() => {
      page = result.current.registerPageContextLayer({
        entity: { type: 'Campaign', id: '7', name: 'Summer' },
        view: { status: 'active', apiKey: 'sk-oops' },
      });
    });
    // …and a tab layers its view on top.
    let tab: ReturnType<typeof result.current.registerPageContextLayer>;
    act(() => {
      tab = result.current.registerPageContextLayer({ view: { tab: 'targetings' } });
    });

    expect(result.current.pageContext).toEqual({
      key: '/campaigns/7',
      title: 'Campaign 7',
      entity: { type: 'Campaign', id: '7', name: 'Summer' },
      view: { status: 'active', tab: 'targetings' }, // secret key sanitized away
    });

    // The tab closes; the page's contribution stays.
    act(() => tab!.dispose());
    expect(result.current.pageContext?.view).toEqual({ status: 'active' });

    // A layer updates IN PLACE (keeps its position under later layers).
    act(() => page!.update({ entity: { type: 'Campaign', id: '7', name: 'Renamed' } }));
    expect(result.current.pageContext?.entity?.name).toBe('Renamed');

    // Without any key-bearing layer there is nothing to publish.
    act(() => page!.dispose());
    act(() => result.current.setPageContext(null));
    expect(result.current.pageContext).toBeNull();
  });

  it('persists pin + width in localStorage, open in sessionStorage; restores on next mount when pinned', () => {
    const w = wrapper({ storagePrefix: 'test' });
    const first = renderHook(() => useAssistant(), { wrapper: w });
    act(() => {
      first.result.current.open();
      first.result.current.setPanelWidth(555);
    });
    expect(localStorage.getItem('test:pinned')).toBe('1');
    expect(localStorage.getItem('test:panel-width')).toBe('555');
    expect(sessionStorage.getItem('test:open')).toBe('1');
    first.unmount();

    // A reload of the same tab: pinned panel comes back, with its width.
    const second = renderHook(() => useAssistant(), { wrapper: wrapper({ storagePrefix: 'test' }) });
    expect(second.result.current.isOpen).toBe(true);
    expect(second.result.current.panelWidth).toBe(555);
  });

  it('an UNPINNED panel is never restored (a dismissed overlay stays dismissed)', () => {
    const w = wrapper({ storagePrefix: 'test' });
    const first = renderHook(() => useAssistant(), { wrapper: w });
    act(() => {
      first.result.current.togglePinned(); // unpin
      first.result.current.open();
    });
    expect(localStorage.getItem('test:pinned')).toBe('0');
    expect(sessionStorage.getItem('test:open')).toBe('1');
    first.unmount();

    const second = renderHook(() => useAssistant(), { wrapper: wrapper({ storagePrefix: 'test' }) });
    expect(second.result.current.isPinned).toBe(false);
    expect(second.result.current.isOpen).toBe(false);
  });

  it('a stored width is clamped on the way back in (no wedged panel)', () => {
    localStorage.setItem('test:panel-width', '99999');
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper({ storagePrefix: 'test' }) });
    expect(result.current.panelWidth).toBeLessThanOrEqual(960);
  });

  it('canChangePinMode=false locks the mode to defaultPinned and leaves the stored preference alone', () => {
    // The user unpinned in an earlier session…
    localStorage.setItem('test:pinned', '0');
    const { result } = renderHook(() => useAssistant(), {
      wrapper: wrapper({ storagePrefix: 'test', canChangePinMode: false }),
    });
    // …but a host that locks the mode stays in its default (pinned) mode.
    expect(result.current.canChangePinMode).toBe(false);
    expect(result.current.isPinned).toBe(true);

    act(() => result.current.togglePinned());
    expect(result.current.isPinned).toBe(true); // no-op

    // The locked mode is never written over the user's choice.
    act(() => result.current.open());
    expect(localStorage.getItem('test:pinned')).toBe('0');
  });

  it('publishes the layout variable only while open AND pinned', () => {
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper() });
    const varValue = () => document.documentElement.style.getPropertyValue(NANNOS_PANEL_WIDTH_VAR);
    expect(varValue()).toBe('0px');
    act(() => result.current.open());
    expect(varValue()).toBe(`${result.current.panelWidth}px`);
    act(() => result.current.togglePinned());
    expect(varValue()).toBe('0px'); // overlay mode reserves no layout space
  });
});

describe('gesture login gating', () => {
  it('open() runs login() when the strategy needs it, and opens only on success', async () => {
    let resolveLogin!: (t: string) => void;
    const auth: NannosAuth = {
      isAuthenticated: () => false,
      getAccessToken: async () => null,
      login: vi.fn(() => new Promise<string>((r) => (resolveLogin = r))),
      logout: () => {},
    };
    const core = createNannos({ auth }, ioFactory);
    const { result } = renderHook(() => useAssistant(), {
      wrapper: ({ children }) => <NannosProvider core={core}>{children}</NannosProvider>,
    });

    act(() => result.current.open('question'));
    expect(auth.login).toHaveBeenCalledTimes(1); // synchronously, in-gesture
    expect(result.current.isOpen).toBe(false); // not before the token exists
    expect(result.current.seededPrompt?.text).toBe('question'); // seed survives the login

    await act(async () => {
      resolveLogin('tok');
      await Promise.resolve();
    });
    expect(result.current.isOpen).toBe(true);
  });
});

describe('keyboard shortcut', () => {
  const press = () =>
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'j', metaKey: true, cancelable: true }));

  it('mod+j toggles when available', () => {
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper() });
    act(() => void press());
    expect(result.current.isOpen).toBe(true);
    act(() => void press());
    expect(result.current.isOpen).toBe(false);
  });

  it('shortcut={false} disables it', () => {
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper({ shortcut: false }) });
    act(() => void press());
    expect(result.current.isOpen).toBe(false);
  });
});

describe('enabled gate + UNAVAILABLE fallback', () => {
  it('outside a provider, useAssistant returns the stable no-op value', () => {
    const { result, rerender } = renderHook(() => useAssistant());
    expect(result.current.isAvailable).toBe(false);
    const first = result.current;
    rerender();
    expect(result.current).toBe(first); // module constant — effect-dep safe
    expect(() => result.current.open('x')).not.toThrow();
  });

  it('enabled={false} yields a null core and isAvailable=false', () => {
    const { result } = renderHook(() => useAssistant(), { wrapper: wrapper({ enabled: false }) });
    expect(result.current.isAvailable).toBe(false);
    expect(result.current.core).toBeNull();
  });
});

describe('i18n seam', () => {
  function Probe() {
    const s = useStrings();
    return <span data-testid="probe">{s['panel.title']}|{format(s['context.label'], { label: 'Campaign 7' })}</span>;
  }

  it('merges host overrides over English defaults; empty overrides fall back per key', () => {
    const { getByTestId } = render(
      <NannosProvider core={makeCore()} strings={{ 'panel.title': 'Assistent', 'context.label': '' }}>
        <Probe />
      </NannosProvider>,
    );
    expect(getByTestId('probe').textContent).toBe('Assistent|Context: Campaign 7');
  });

  it('format leaves unknown placeholders intact', () => {
    expect(format('Hi {name}, {unknown}', { name: 'Erik' })).toBe('Hi Erik, {unknown}');
  });
});

describe('useNannosPageContext', () => {
  it('publishes while mounted (following value changes), clears on unmount', () => {
    const core = makeCore();
    let assistant: ReturnType<typeof useAssistant> | null = null;
    function Probe() {
      assistant = useAssistant();
      return null;
    }
    function Declarer({ path }: { path: string }) {
      // Inline object on purpose: the hook must key on VALUE, not identity.
      useNannosPageContext({ key: path, title: `Page ${path}` });
      return null;
    }
    const ui = (declarer: ReactNode) => (
      <NannosProvider core={core}>
        <Probe />
        {declarer}
      </NannosProvider>
    );

    const view = render(ui(<Declarer path="/a" />));
    expect(assistant!.pageContext).toEqual({ key: '/a', title: 'Page /a' });

    // Navigation: the same declarer re-renders with the next page.
    view.rerender(ui(<Declarer path="/b" />));
    expect(assistant!.pageContext).toEqual({ key: '/b', title: 'Page /b' });

    // The declarer unmounts (a page with no context) → cleared, not stale.
    view.rerender(ui(null));
    expect(assistant!.pageContext).toBeNull();
  });
});

describe('useNannosPageReader', () => {
  it("answers the agent's read_current_page pull through the core, alongside the page context", async () => {
    const core = makeCore();
    function Declarer() {
      useNannosPageContext({ key: '/campaigns/7', title: 'Campaign 7' });
      useNannosPageReader('rows', () => ['Geo CH', 'Age 18-35']);
      return null;
    }
    const view = render(
      <NannosProvider core={core}>
        <Declarer />
      </NannosProvider>,
    );

    const result = await core.runClientAction({ kind: 'read_current_page' });
    expect(result.ok).toBe(true);
    const content = JSON.parse((result as { content: string }).content);
    expect(content.page).toEqual({ key: '/campaigns/7', title: 'Campaign 7' });
    expect(content.rows).toEqual(['Geo CH', 'Age 18-35']);

    // The reader unregisters with its component; the page snapshot remains.
    view.rerender(<NannosProvider core={core}>{null}</NannosProvider>);
    const after = await core.runClientAction({ kind: 'read_current_page' });
    const afterContent = JSON.parse((after as { content: string }).content);
    expect(afterContent.rows).toBeUndefined();
  });

  it('every read carries the screen outline by default, under the reserved key', async () => {
    const core = makeCore();
    render(
      <NannosProvider core={core}>
        <main>
          <h1>Campaign 7</h1>
          <p>12 line items running</p>
        </main>
      </NannosProvider>,
    );
    const result = await core.runClientAction({ kind: 'read_current_page' });
    const content = JSON.parse((result as { content: string }).content);
    expect(content.screen).toContain('# Campaign 7');
    expect(content.screen).toContain('12 line items running');
  });

  it('screenOutline={false} sends only page context + readers, no DOM walk', async () => {
    const core = makeCore();
    render(
      <NannosProvider core={core} screenOutline={false}>
        <main>
          <h1>Campaign 7</h1>
        </main>
      </NannosProvider>,
    );
    const result = await core.runClientAction({ kind: 'read_current_page' });
    const content = JSON.parse((result as { content: string }).content);
    expect(content.screen).toBeUndefined();
  });
});

describe('StrictMode remount', () => {
  it('double-invoked effects reconnect cleanly and state survives', () => {
    const core = makeCore();
    const seen: boolean[] = [];
    function Probe() {
      seen.push(useAssistant().isAvailable);
      return null;
    }
    render(
      <StrictMode>
        <NannosProvider core={core}>
          <Probe />
        </NannosProvider>
      </StrictMode>,
    );
    expect(seen.every(Boolean)).toBe(true);
  });
});
