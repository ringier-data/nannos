import { AssistantPanel } from '@nannos/embed-sdk/panel';

/**
 * The console's full-page chat: the SDK panel in LIGHT-DOM mode (styled by the
 * console's own Tailwind sheet), two-pane with the conversation list, no panel
 * header (the dashboard shell provides the chrome). It reuses the layout-level
 * chat scope, so a turn keeps streaming when the user navigates away.
 */
export function ChatPage() {
  return (
    <div className="h-full">
      <AssistantPanel shadow={false} showConversationList header={false} />
    </div>
  );
}
