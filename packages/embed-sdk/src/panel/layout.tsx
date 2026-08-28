/**
 * Which surface the panel is laid out for. The SDK's components were tuned for
 * a narrow docked panel; the console's full-page chat mounts the same tree at
 * 1000px+ where those choices invert — edge-to-edge text reads as a wall, and
 * the grey activity micro-lines become the most prominent thing on screen.
 * `'page'` caps the column at a reading width and folds the activity stream
 * into one disclosure per turn. Default `'panel'` keeps every embed unchanged.
 */
import { createContext, useContext, type ReactNode } from 'react';

export type PanelLayout = 'panel' | 'page';

const PanelLayoutContext = createContext<PanelLayout>('panel');

export function usePanelLayout(): PanelLayout {
  return useContext(PanelLayoutContext);
}

export function PanelLayoutProvider({ layout, children }: { layout: PanelLayout; children: ReactNode }) {
  return <PanelLayoutContext.Provider value={layout}>{children}</PanelLayoutContext.Provider>;
}

/** The reading column every page-mode block shares, so thread and composer align. */
export const PAGE_COLUMN = 'mx-auto w-full max-w-3xl px-6';
