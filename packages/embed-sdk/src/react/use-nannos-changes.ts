import { useCallback, useMemo, useSyncExternalStore } from 'react';
import type { AppliedChange, ChangeTarget } from '../core/change-store';
import { useAssistant } from './provider';

const NONE: AppliedChange[] = [];
const noop = () => () => {};

/**
 * The assistant's unsaved changes on one on-screen object, live — for a host's review
 * bar next to the form's Save ("Nannos changed 3 fields · Undo all"). `target` is the
 * object's manifest id, the one `createNannosForm` derives (`deriveObjectId`).
 * Empty without a provider or a target.
 */
export function useNannosChanges(target: ChangeTarget | null): {
  changes: AppliedChange[];
  undoAll: () => Promise<boolean>;
} {
  const store = useAssistant().core?.changes ?? null;
  const version = useSyncExternalStore(
    store?.subscribe ?? noop,
    store?.getVersion ?? (() => 0),
    () => 0,
  );
  const type = target?.type;
  const id = target?.id;
  const changes = useMemo(
    () => (store && type && id !== undefined ? store.get({ type, id }) : NONE),
    // `version` is the store's change stamp: a new value means a new list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [store, type, id, version],
  );
  const undoAll = useCallback(async () => {
    const results = await Promise.all(changes.map((c) => c.undo()));
    return results.every(Boolean);
  }, [changes]);
  return { changes, undoAll };
}
