import { useCallback, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

/**
 * Which groups have a resource as a group default, plus the user's toggles.
 *
 * There is no "which groups default to this resource" read — the group owns its
 * default list — so this asks each group in turn. Only groups that already hold a
 * grant can have a default (the backend refuses one without it), so the saved
 * grant set is the whole list worth asking, however many groups the user
 * belongs to.
 *
 * `granted` drops a default the moment its grant is revoked in the dialog: a
 * default without a grant behind it is not a state the backend accepts.
 */
export function useGroupDefaults({
  queryKey,
  groupIds,
  isDefaultFor,
  granted,
}: {
  /** Names this resource; the grant ids are appended. */
  queryKey: readonly unknown[];
  /** The groups holding a SAVED grant. */
  groupIds: readonly number[];
  /** Whether the resource is a default of this group. */
  isDefaultFor: (groupId: number) => Promise<boolean>;
  /** Whether the group holds a grant in the dialog's current (edited) state. */
  granted: (groupId: number) => boolean;
}) {
  const ids = useMemo(() => [...groupIds].sort((a, b) => a - b), [groupIds]);

  const { data: savedDefaults, isLoading } = useQuery({
    queryKey: [...queryKey, ids],
    queryFn: async () => {
      const found: number[] = [];
      await Promise.all(
        ids.map(async (groupId) => {
          try {
            if (await isDefaultFor(groupId)) found.push(groupId);
          } catch {
            // One unreadable group must not blank the others out.
          }
        }),
      );
      return found;
    },
    enabled: ids.length > 0,
  });

  const initial = useMemo(() => new Set(savedDefaults ?? []), [savedDefaults]);
  const [edits, setEdits] = useState<ReadonlyMap<number, boolean>>(new Map());

  const defaults = new Set(
    [...initial, ...edits.keys()].filter(
      (groupId) => (edits.get(groupId) ?? initial.has(groupId)) && granted(groupId),
    ),
  );
  const added = [...defaults].filter((groupId) => !initial.has(groupId));
  const removed = [...initial].filter((groupId) => !defaults.has(groupId));

  const setDefault = useCallback((groupId: number, on: boolean) => {
    setEdits((prev) => new Map(prev).set(groupId, on));
  }, []);

  return {
    defaults,
    added,
    removed,
    hasChanges: added.length > 0 || removed.length > 0,
    setDefault,
    isLoading: ids.length > 0 && isLoading,
  };
}
