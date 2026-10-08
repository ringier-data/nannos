import { createNannosActions, type ObjectAction, type RouteId } from '@nannos/embed-sdk';
import { consoleObjectTypes } from './consoleObjects';

const useConsoleActions = createNannosActions(consoleObjectTypes);

/**
 * What the user could click on an object shown read-only — Edit, a New button, a
 * dialog with no route — offered to the assistant as `invoke` actions. Mount it where
 * the matching `<NannosForm>` is NOT mounted (the view-mode branch): both register the
 * same `type:id`. An action opens or starts something; it never saves.
 */
export function NannosActions(props: {
  type: keyof typeof consoleObjectTypes & string;
  id: RouteId;
  parentId?: RouteId;
  actions: Record<string, ObjectAction>;
}) {
  useConsoleActions(props);
  return null;
}
