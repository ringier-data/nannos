/**
 * The two halves every "share with groups" dialog is built from.
 *
 * - `GrantedGroupsTable` lists the groups that hold a grant — all of them, from the
 *   unpaged permissions list plus the dialog's edits — so a grant is always visible
 *   and editable, whichever page of groups is showing below it.
 * - `GroupGrantPicker` is the group list a grant is added from: searched and paged
 *   on the server, because a user (or an administrator, org-wide) can belong to far
 *   more groups than a dialog should load and filter in the browser.
 */
import { useState, type ReactNode } from 'react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { HelpCircle, Loader2, Search, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Pagination } from '@/components/admin/Pagination';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { listGroupsApiV1AdminGroupsGet } from '@/api/generated/sdk.gen';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { useMyGroupsPicker } from '@/hooks/use-my-groups-picker';
import type { GrantedGroup, GrantRole, GrantTarget } from '@/hooks/use-group-grants';
import { getErrorMessage } from '@/lib/utils';

const PAGE_SIZE = 10;

/**
 * Whose groups to offer. `mine` — the caller's own groups — is what every dialog
 * shares to; `all` is the org-wide admin list, for an administrator in admin mode.
 */
export type GroupScope = 'mine' | 'all';

interface CandidateGroup {
  id: number;
  name: string;
  description?: string | null;
  member_count?: number;
}

async function fetchAdminGroupPage(
  search: string,
  page: number,
  signal: AbortSignal,
): Promise<{ groups: CandidateGroup[]; total: number }> {
  const { data, error } = await listGroupsApiV1AdminGroupsGet({
    query: { page, limit: PAGE_SIZE, search: search || undefined },
    signal,
  });
  if (error) throw error;
  return {
    groups: data!.data.map((g) => ({
      id: g.id,
      name: g.name,
      description: g.description,
      member_count: g.member_count ?? g.members?.length,
    })),
    total: data!.meta.total,
  };
}

function memberLabel(count: number | undefined) {
  if (count === undefined) return null;
  return `${count} ${count === 1 ? 'member' : 'members'}`;
}

interface GroupGrantPickerProps {
  scope?: GroupScope;
  /** The group's role in the dialog's current state, if it holds a grant. */
  roleOf: (groupId: number) => GrantRole | undefined;
  onGrant: (group: GrantTarget, role: GrantRole) => void;
  /** Shown when the caller has no groups at all (not when a search matches none). */
  emptyMessage: string;
}

export function GroupGrantPicker({ scope = 'mine', roleOf, onGrant, emptyMessage }: GroupGrantPickerProps) {
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const debouncedSearch = useDebouncedValue(search);

  // Both keep the previous page on screen while the next loads (`keepPreviousData`);
  // otherwise the table unmounts on every keystroke, which resizes the dialog and
  // takes focus out of the search box mid-word.
  const mine = useMyGroupsPicker({
    enabled: scope === 'mine',
    search: debouncedSearch,
    page,
    pageSize: PAGE_SIZE,
  });
  const all = useQuery({
    queryKey: ['admin-group-grant-candidates', { search: debouncedSearch, page }],
    queryFn: ({ signal }) => fetchAdminGroupPage(debouncedSearch, page, signal),
    enabled: scope === 'all',
    retry: false,
    placeholderData: keepPreviousData,
  });

  const groups: CandidateGroup[] = scope === 'all' ? (all.data?.groups ?? []) : mine.groups;
  const total = (scope === 'all' ? all.data?.total : mine.total) ?? 0;
  const isLoading = scope === 'all' ? all.isPending : mine.isPending;
  const error = scope === 'all' ? all.error : mine.error;
  const searching = Boolean(search || debouncedSearch);

  const handleSearchChange = (value: string) => {
    setSearch(value);
    setPage(1);
  };

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium text-muted-foreground">Add groups</h3>

      {error ? (
        <p className="py-4 text-center text-sm text-destructive">
          Failed to load groups: {getErrorMessage(error)}
        </p>
      ) : isLoading ? (
        <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading groups…
        </div>
      ) : total === 0 && !searching ? (
        // An empty *search result* must not hide the search box, or there is no way
        // left to clear the term. Only a genuinely empty list does.
        <p className="py-4 text-center text-sm text-muted-foreground">{emptyMessage}</p>
      ) : (
        <>
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search groups…"
              value={search}
              onChange={(e) => handleSearchChange(e.target.value)}
              className="pl-9"
            />
          </div>

          <div className="border rounded-md">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Group</TableHead>
                  <TableHead className="w-[180px]">Access</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {groups.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={2} className="text-center py-6 text-muted-foreground">
                      No groups match your search.
                    </TableCell>
                  </TableRow>
                ) : (
                  groups.map((group) => {
                    const role = roleOf(group.id);
                    const members = memberLabel(group.member_count);
                    return (
                      <TableRow key={group.id}>
                        <TableCell>
                          <div className="font-medium">{group.name}</div>
                          {(members || group.description) && (
                            <div className="text-xs text-muted-foreground">
                              {[members, group.description].filter(Boolean).join(' • ')}
                            </div>
                          )}
                        </TableCell>
                        <TableCell>
                          {role ? (
                            // Edited in "Groups with access" above, so there is one
                            // place a grant changes rather than two that must agree.
                            <span className="text-sm text-muted-foreground">
                              Has access ({role === 'write' ? 'Write' : 'Read'})
                            </span>
                          ) : (
                            <Select
                              value=""
                              onValueChange={(value) =>
                                onGrant(
                                  { id: group.id, name: group.name, memberCount: group.member_count },
                                  value as GrantRole,
                                )
                              }
                            >
                              <SelectTrigger className="w-[160px]">
                                <SelectValue placeholder="No access" />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="read">Read</SelectItem>
                                <SelectItem value="write">Write</SelectItem>
                              </SelectContent>
                            </Select>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })
                )}
              </TableBody>
            </Table>
          </div>

          <Pagination page={page} limit={PAGE_SIZE} total={total} onPageChange={setPage} />
        </>
      )}
    </div>
  );
}

interface GrantedGroupsTableProps {
  grants: GrantedGroup[];
  /** A new role, or `null` to revoke — the same signature as `useGroupGrants().setRole`. */
  onRoleChange: (group: GrantTarget, role: GrantRole | null) => void;
  /** What read and write mean for this resource. */
  roleHelp: ReactNode;
  /** A column between the role and the remove button — the group-default toggle. */
  extraColumn?: {
    header: ReactNode;
    className?: string;
    cell: (grant: GrantedGroup) => ReactNode;
  };
  emptyMessage: string;
}

export function GrantedGroupsTable({
  grants,
  onRoleChange,
  roleHelp,
  extraColumn,
  emptyMessage,
}: GrantedGroupsTableProps) {
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium text-muted-foreground">Groups with access</h3>
      {grants.length === 0 ? (
        <p className="rounded-md border border-dashed py-4 text-center text-sm text-muted-foreground">
          {emptyMessage}
        </p>
      ) : (
        <div className="border rounded-md">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Group</TableHead>
                <TableHead className="w-[180px]">
                  <div className="flex items-center gap-1">
                    Permission
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <HelpCircle className="h-3.5 w-3.5 text-muted-foreground cursor-help" />
                        </TooltipTrigger>
                        <TooltipContent>
                          <p className="max-w-xs">{roleHelp}</p>
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  </div>
                </TableHead>
                {extraColumn && <TableHead className={extraColumn.className}>{extraColumn.header}</TableHead>}
                <TableHead className="w-[60px]" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {grants.map((grant) => {
                const members = memberLabel(grant.memberCount);
                const target = { id: grant.groupId, name: grant.name, memberCount: grant.memberCount };
                return (
                  <TableRow key={grant.groupId}>
                    <TableCell>
                      <div className="font-medium">{grant.name}</div>
                      {members && <div className="text-xs text-muted-foreground">{members}</div>}
                    </TableCell>
                    <TableCell>
                      <Select
                        value={grant.role}
                        onValueChange={(value) => onRoleChange(target, value as GrantRole)}
                      >
                        <SelectTrigger className="w-[160px]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="read">Read</SelectItem>
                          <SelectItem value="write">Write</SelectItem>
                        </SelectContent>
                      </Select>
                    </TableCell>
                    {extraColumn && <TableCell>{extraColumn.cell(grant)}</TableCell>}
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Remove access for ${grant.name}`}
                        onClick={() => onRoleChange(target, null)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
