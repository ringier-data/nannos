import { createContext, useContext, type ReactNode } from 'react';

/**
 * The element Radix Portals (Dialog, Popover, Select, DropdownMenu, Tooltip)
 * should render INTO.
 *
 * Radix defaults portals to `document.body`. In the embed the widget lives in a
 * Shadow DOM, so that default escapes the shadow root into the host's light DOM —
 * where our adopted `theme.css` doesn't reach and the content is detached from the
 * widget panel. The result: the settings dialog, the "Connected" agent-info
 * popover, model/select dropdowns etc. open OUTSIDE the widget, unstyled and
 * effectively invisible — so they look clickable but "do nothing".
 *
 * Providing the shadow container here keeps portalled UI inside the shadow root:
 * styled, positioned within the panel, and interactive. `undefined` (the console,
 * or any non-shadow mount) falls through to Radix's `document.body` default.
 */
const PortalContainerContext = createContext<HTMLElement | undefined>(undefined);

export function PortalContainerProvider({
  container,
  children,
}: {
  container: HTMLElement | null | undefined;
  children: ReactNode;
}) {
  return (
    <PortalContainerContext.Provider value={container ?? undefined}>
      {children}
    </PortalContainerContext.Provider>
  );
}

/** The shadow-root portal container, or `undefined` to use Radix's default (body). */
export function usePortalContainer(): HTMLElement | undefined {
  return useContext(PortalContainerContext);
}
