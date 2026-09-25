import { useCallback, useMemo, useState } from 'react';

export type GrantRole = 'read' | 'write';

/** A group the resource is (or is about to be) shared with. */
export interface GrantedGroup {
  groupId: number;
  name: string;
  role: GrantRole;
  /** Known when the group was picked from the group list; permission rows don't carry it. */
  memberCount?: number;
}

/** The group being granted, as the caller knows it. */
export interface GrantTarget {
  id: number;
  name: string;
  memberCount?: number;
}

/** The shape every `.../permissions` endpoint answers with. */
interface GrantRow {
  user_group_id: number;
  user_group_name: string;
  permissions?: readonly string[];
}

function roleFromPermissions(permissions: readonly string[] | undefined): GrantRole | null {
  if (permissions?.includes('write')) return 'write';
  if (permissions?.includes('read')) return 'read';
  return null;
}

/**
 * The grant set of a permissions dialog: the saved rows plus the user's edits.
 *
 * Saving REPLACES the whole set, so `rows` must be the unpaged permissions list —
 * a page of it would silently revoke every grant past that page. The candidate
 * groups a user can add are paged separately (`GroupGrantPicker`); a grant stays
 * listed and editable here whether or not its group is on the page being shown,
 * which is what the rows' `user_group_name` is for.
 *
 * Edits are kept apart from the rows rather than copied into state when they
 * load, so there is no effect to re-sync, and `hasChanges` is a comparison
 * rather than a flag to remember to set. Mount the hook in a component that
 * lives only while the dialog is open and the edits reset with it.
 */
export function useGroupGrants(rows: readonly GrantRow[] | undefined) {
  const [edits, setEdits] = useState<ReadonlyMap<number, GrantedGroup | null>>(new Map());

  const initial = useMemo(() => {
    const saved = new Map<number, GrantedGroup>();
    rows?.forEach((row) => {
      const role = roleFromPermissions(row.permissions);
      if (role) saved.set(row.user_group_id, { groupId: row.user_group_id, name: row.user_group_name, role });
    });
    return saved;
  }, [rows]);

  const current = useMemo(() => {
    const next = new Map(initial);
    edits.forEach((grant, groupId) => {
      if (grant) next.set(groupId, grant);
      else next.delete(groupId);
    });
    return next;
  }, [initial, edits]);

  const grants = useMemo(
    () => [...current.values()].sort((a, b) => a.name.localeCompare(b.name)),
    [current],
  );

  const hasChanges =
    current.size !== initial.size ||
    [...current].some(([groupId, grant]) => initial.get(groupId)?.role !== grant.role);

  /** Grant, change or (with `null`) revoke a group's access. */
  const setRole = useCallback((group: GrantTarget, role: GrantRole | null) => {
    setEdits((prev) => {
      const next = new Map(prev);
      next.set(
        group.id,
        role ? { groupId: group.id, name: group.name, role, memberCount: group.memberCount } : null,
      );
      return next;
    });
  }, []);

  /** The body every permissions PUT takes: the whole set, write implying read. */
  const toPermissions = () =>
    grants.map((grant) => ({
      user_group_id: grant.groupId,
      permissions: (grant.role === 'write' ? ['read', 'write'] : ['read']) as GrantRole[],
    }));

  return {
    grants,
    roleOf: (groupId: number) => current.get(groupId)?.role,
    setRole,
    hasChanges,
    toPermissions,
  };
}
