import { AssistantPanel } from '@nannos/embed-sdk/panel';
import { useAuth } from '@/contexts/AuthContext';

/**
 * The console's full-page chat: the SDK panel in LIGHT-DOM mode (styled by the
 * console's own Tailwind sheet), two-pane with the conversation list, no panel
 * header (the dashboard shell provides the chrome). It reuses the layout-level
 * chat scope, so a turn keeps streaming when the user navigates away.
 *
 * Developer mode is gated exactly like every other admin surface in the console
 * (see `AdminRoute`): administrator AND admin mode on. Turning admin mode off
 * takes the dev chrome with it, so a demo never shows it by accident.
 *
 * `devMode` is passed EXPLICITLY, never left to the SDK's `localStorage` hatch:
 * an explicit boolean wins over it, so a non-admin who sets `nannos:dev` by
 * hand still gets no inspector. Within the gate, on/off lives on the
 * inspector's own bar in this view — the SDK remembers it per browser, so it
 * survives navigating away from the chat page and back.
 */
export function ChatPage() {
  const { isAdmin, adminMode } = useAuth();

  return (
    <div className="h-full">
      <AssistantPanel
        shadow={false}
        showConversationList
        header={false}
        devMode={isAdmin && adminMode}
        layout="page"
      />
    </div>
  );
}
