import { useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { listMyGroupsApiV1GroupsGet } from '@/api/generated/sdk.gen';
import { listMyGroupsApiV1GroupsGetQueryKey } from '@/api/generated/@tanstack/react-query.gen';
import { totalCountFrom } from '@/api/total-count';
import type { SearchableSelectOption } from '@/components/SearchableSelect';

const PICKER_PAGE_SIZE = 20;

/**
 * One server-searched page of the caller's own groups, shaped for a
 * `SearchableSelect`.
 *
 * `search` is the term the picker hands back (it debounces it itself). The body
 * is a bare array, so the match count comes from `X-Total-Count`, read off the
 * generated operation because the tanstack wrapper drops the response; the
 * trailing key element keeps this `{rows, total}` entry apart from the
 * plain-array ones other callers cache under the same `_id`.
 */
export function useMyGroupsPicker({
  enabled = true,
  search: controlledSearch,
  page = 1,
  pageSize = PICKER_PAGE_SIZE,
}: {
  enabled?: boolean;
  /**
   * An already-debounced term owned by the caller, for a list that pages rather
   * than a `SearchableSelect` (the permission dialogs' `GroupGrantPicker`). When
   * given, `onSearchChange` is unused.
   */
  search?: string;
  page?: number;
  pageSize?: number;
} = {}) {
  const [internalSearch, setSearch] = useState('');
  const search = controlledSearch ?? internalSearch;
  const query = { page, limit: pageSize, search: search || undefined };

  const { data, isFetching, isPending, error } = useQuery({
    queryKey: [...listMyGroupsApiV1GroupsGetQueryKey({ query }), 'with-total'] as const,
    queryFn: async ({ signal }) => {
      const { data: rows, response } = await listMyGroupsApiV1GroupsGet({
        query,
        signal,
        throwOnError: true,
      });
      return { rows, total: totalCountFrom(response, rows.length) };
    },
    enabled,
    placeholderData: keepPreviousData,
  });

  const groups = data?.rows ?? [];
  const options: SearchableSelectOption[] = groups.map((g) => ({
    value: String(g.id),
    label: g.name,
    hint: g.description ?? undefined,
  }));

  return {
    groups,
    options,
    total: data?.total,
    isLoading: isFetching,
    /** No page has arrived yet (unlike `isLoading`, false while a next page loads). */
    isPending: enabled && isPending,
    error,
    search,
    onSearchChange: setSearch,
  };
}
