import { useEffect, useRef, useState } from 'react';
import { mount } from '../element';
import type { NannosCore } from '../core';
import type { NannosHostAdapter } from './adapter';

export interface NannosWidgetProps {
  /** The core to render — pass `useNannos()` from your <NannosProvider>. Null (e.g.
   *  provider disabled) renders nothing. Explicit (not context) so the widget has
   *  no dependency on the provider context crossing package entry points. */
  core: NannosCore | null;
  /** Host adapter (auth facts / REST overrides) forwarded to the mounted widget. */
  adapter?: NannosHostAdapter;
  /** Start with the panel open (default false — launcher only). */
  defaultOpen?: boolean;
  /** Launcher glyph (default 💬). */
  launcherLabel?: string;
  /** Brand color for the launcher button (default #7c3aed). Set to the host's
   *  brand so the widget reads as native rather than a bolt-on. (Deeper panel
   *  theming via the shadow-DOM CSS tokens is a separate, planned surface.) */
  accent?: string;
}

const Z = 2147483000;
const panelStyle: React.CSSProperties = {
  position: 'fixed',
  bottom: 88,
  right: 24,
  width: 400,
  height: 640,
  maxHeight: 'calc(100vh - 112px)',
  maxWidth: 'calc(100vw - 48px)',
  zIndex: Z,
  borderRadius: 12,
  overflow: 'hidden',
  boxShadow: '0 12px 48px rgba(0,0,0,0.22)',
  background: 'transparent',
};
const launcherStyle: React.CSSProperties = {
  position: 'fixed',
  bottom: 24,
  right: 24,
  width: 56,
  height: 56,
  borderRadius: '50%',
  border: 'none',
  cursor: 'pointer',
  fontSize: 24,
  lineHeight: '56px',
  color: '#fff',
  background: '#7c3aed',
  boxShadow: '0 6px 20px rgba(0,0,0,0.25)',
  zIndex: Z + 1,
};

/**
 * Drop-in floating chat widget: a launcher button and a panel that mounts the
 * Nannos chat (Shadow-DOM isolated, via `mount()`) when opened. Reads the shared
 * core (pass `useNannos()`). For a bespoke layout, skip this and call `mount()`
 * into your own sized container.
 */
export function NannosWidget({ core, adapter, defaultOpen = false, launcherLabel = '💬', accent = '#7c3aed' }: NannosWidgetProps) {
  // Open state lives on the CORE (core.onOpenChange), so a host's own launcher —
  // or `useNannos().open(prompt)` from anywhere in the tree — drives the same
  // panel. This component just mirrors it and toggles via the core.
  const [open, setOpen] = useState(core?.isOpen ?? defaultOpen);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!core) return;
    if (defaultOpen && !core.isOpen) core.open();
    return core.onOpenChange(setOpen);
  }, [core, defaultOpen]);

  // Read through a ref and EXCLUDED from the mount effect's deps: hosts naturally
  // pass `adapter` as an inline object, and tearing the whole panel down (shadow
  // mount, socket handshake, conversation state) on every host render is never
  // what they mean. The adapter is captured when the panel opens; change it by
  // closing/reopening or remounting with a `key`.
  const adapterRef = useRef(adapter);
  adapterRef.current = adapter;

  useEffect(() => {
    // Re-mount on each open: we render a FRESH panel div each open (React
    // discards it on close) rather than toggling display on a reused host.
    if (!open || !core || !panelRef.current) return;
    const { unmount } = mount(core, panelRef.current, { adapter: adapterRef.current });
    return () => unmount();
  }, [open, core]);

  if (!core) return null;

  // The launcher click is a user gesture — the one place a PKCE popup is allowed.
  // If a self-login strategy isn't authenticated yet, run login() HERE (called
  // synchronously so window.open stays in-gesture), then open on success. This is
  // what lets a PKCE host use the built-in launcher with zero custom auth code.
  const onLauncherClick = () => {
    if (core.needsLogin()) {
      core.login().then(() => core.open()).catch(() => {/* status → authError; host renders it */});
    } else {
      core.toggle();
    }
  };

  return (
    <>
      {open && <div ref={panelRef} style={panelStyle} />}
      <button
        type="button"
        onClick={onLauncherClick}
        aria-label={open ? 'Close assistant' : 'Open assistant'}
        style={{ ...launcherStyle, background: accent }}
      >
        {open ? '×' : launcherLabel}
      </button>
    </>
  );
}
