import { AssistantPanel } from '@nannos/embed-sdk/panel';
import { useNannosDevMode } from '@/components/nannos/NannosDevMode';

/**
 * The console's full-page chat: the SDK panel in LIGHT-DOM mode (styled by the
 * console's own Tailwind sheet), two-pane with the conversation list, no panel
 * header (the dashboard shell provides the chrome). It reuses the layout-level
 * chat scope, so a turn keeps streaming when the user navigates away.
 *
 * `devMode` is passed EXPLICITLY, never left to the SDK's localStorage hatch:
 * an explicit boolean wins over it, so a non-admin who sets `nannos:dev` by
 * hand still gets no inspector. See `NannosDevMode`.
 */
export function ChatPage() {
  const { enabled: devMode } = useNannosDevMode();

  return (
    <div className="h-full">
      <AssistantPanel shadow={false} showConversationList header={false} devMode={devMode} />
    </div>
  );
}
