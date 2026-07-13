import { Toaster } from 'sonner';
import type { NannosCore } from '../core';
import { HostAdapterProvider, type NannosHostAdapter } from './adapter';
import { PortalContainerProvider } from '@/lib/portal-container';
import { ChatAppWrapper } from './chat';

export interface ChatWidgetProps {
  core: NannosCore;
  adapter?: NannosHostAdapter;
  /** Compact single-pane layout (hides the conversation sidebar). Defaults to
   *  true — the batteries-included widget targets narrow embedded surfaces. */
  compact?: boolean;
  /** Shadow-root element that Radix portals should render into (see mount()).
   *  Omit for light-DOM mounts (portals fall back to document.body). */
  portalContainer?: HTMLElement;
}

/**
 * Batteries-included embed surface: host adapter + socket/chat providers + the
 * extracted console ChatApp (one UI — console-frontend consumes the same
 * components). Hosts needing custom composition use the exported providers and
 * components directly instead.
 */
export function ChatWidget({ core, adapter, compact = true, portalContainer }: ChatWidgetProps) {
  return (
    <HostAdapterProvider core={core} adapter={adapter}>
      <PortalContainerProvider container={portalContainer}>
        <ChatAppWrapper compact={compact} />
        <Toaster position="top-right" />
      </PortalContainerProvider>
    </HostAdapterProvider>
  );
}
