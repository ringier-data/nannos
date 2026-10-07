import { useEffect, useRef, useState, type CSSProperties, type PointerEvent } from 'react';
import { NANNOS_PANEL_WIDTH_VAR, clampPanelWidth, useAssistant } from '../react/provider';
import { ASSISTANT_ATTRIBUTE } from '../react/client-actions';
import { PortalContainerProvider } from '../lib/portal-container';
import { AssistantPanel, type AssistantPanelProps } from './assistant-panel';

export interface AssistantDockProps extends AssistantPanelProps {
  /** Stacking order of the fixed container. Keep it under the host's modals. Default 40. */
  zIndex?: number;
  /** Distance from the top of the viewport, e.g. under a fixed app bar. Default 0. */
  top?: number | string;
  /** Extra class on the fixed container (e.g. the host's border/background utilities). */
  className?: string;
  /** Inline style merged over the container defaults. */
  style?: CSSProperties;
}

/**
 * The panel docked at the right edge of the viewport — the container every
 * single-page host otherwise writes itself (the cockpit's `NannosPanel`).
 *
 * Renders once the assistant is available and has been opened; closed, it stays
 * mounted but hidden (its chat keeps its connection). Pinned, it is flush
 * with the page: the provider already publishes `--nannos-panel-width` on
 * `<html>`, so the host gives its content `padding-right:
 * var(--nannos-panel-width, 0px)` and the page yields the same width. Unpinned,
 * it floats over the page with a shadow. The left edge is a drag handle; a drag
 * writes the width straight to the DOM per frame and commits once on release.
 *
 * The container is marked as the assistant (`isAssistantElement`) and the panel's
 * own popovers portal INTO it, so a host's modal can tell a click in the assistant
 * from a click on the page behind it and stay open while the user asks for help.
 */
export function AssistantDock({ zIndex = 40, top = 0, className, style, ...panelProps }: AssistantDockProps) {
  const { isAvailable, isOpen, isPinned, panelWidth, setPanelWidth } = useAssistant();
  const containerRef = useRef<HTMLDivElement>(null);
  const liveWidth = useRef(panelWidth);
  const [portalHost, setPortalHost] = useState<HTMLDivElement | null>(null);

  // The committed width is the drag's next starting point. The render below writes
  // it back to the container, replacing whatever the drag set inline.
  useEffect(() => {
    liveWidth.current = panelWidth;
  }, [panelWidth]);

  // Mounted on the first open, then only hidden: unmounting the panel tore down its
  // chat scope and socket, so every reopen reconnected, re-bound and reloaded the
  // conversation, and a reply still streaming when the user closed the dock was lost.
  const [opened, setOpened] = useState(isOpen);
  if (isOpen && !opened) setOpened(true);

  if (!isAvailable || !opened) return null;

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const move = (e: globalThis.PointerEvent) => {
      const width = clampPanelWidth(window.innerWidth - e.clientX);
      liveWidth.current = width;
      if (containerRef.current) containerRef.current.style.width = `${width}px`;
      if (isPinned) document.documentElement.style.setProperty(NANNOS_PANEL_WIDTH_VAR, `${width}px`);
    };
    const end = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', end);
      handle.removeEventListener('pointercancel', end);
      setPanelWidth(liveWidth.current);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
  };

  return (
    <div
      ref={containerRef}
      className={className}
      data-nannos-ignore
      {...{ [ASSISTANT_ATTRIBUTE]: '' }}
      hidden={!isOpen}
      inert={!isOpen}
      style={{
        position: 'fixed',
        top,
        right: 0,
        bottom: 0,
        width: `${panelWidth}px`,
        zIndex,
        display: 'flex',
        flexDirection: 'column',
        boxShadow: isPinned ? 'none' : '0 10px 38px rgba(0, 0, 0, 0.25)',
        ...style,
        ...(isOpen ? {} : { display: 'none' }),
      }}
    >
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize assistant panel"
        onPointerDown={onPointerDown}
        style={{
          position: 'absolute',
          left: -3,
          top: 0,
          bottom: 0,
          width: 6,
          cursor: 'col-resize',
          zIndex: 1,
          touchAction: 'none',
        }}
      />
      <PortalContainerProvider container={portalHost}>
        <AssistantPanel {...panelProps} />
      </PortalContainerProvider>
      <div ref={setPortalHost} />
    </div>
  );
}
