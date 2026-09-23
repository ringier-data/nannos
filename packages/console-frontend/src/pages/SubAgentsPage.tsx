import { useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus } from 'lucide-react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { SubAgentList } from '@/components/subagents/SubAgentList';
import {
  EMPTY_SUB_AGENT_FILTERS,
  type ScopeFilter,
  type SubAgentFilters,
} from '@/components/subagents/types';
import { Pagination } from '@/components/admin/Pagination';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import {
  consoleListSubAgentsOptions,
  listPendingApprovalsApiV1SubAgentsPendingGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { SubAgentListItem, SubAgentListResponse } from '@/api/generated/types.gen';
import { useAuth } from '@/contexts/AuthContext';

const PAGE_SIZE = 20;

export function SubAgentsPage() {
  const navigate = useNavigate();
  const [scope, setScope] = useState<ScopeFilter>('all');
  const [filters, setFilters] = useState<SubAgentFilters>(EMPTY_SUB_AGENT_FILTERS);
  const [page, setPage] = useState(1);
  const debouncedSearch = useDebouncedValue(filters.search);
  const { adminMode } = useAuth();

  // Any facet change re-pages from the start, or a narrower filter lands on a
  // page that no longer exists.
  const handleFiltersChange = (next: SubAgentFilters) => {
    setFilters(next);
    setPage(1);
  };

  const handleScopeChange = (next: ScopeFilter) => {
    setScope(next);
    setPage(1);
  };

  // Derive the effective scope so the approval queue isn't shown once admin mode is off,
  // without resetting state in an effect.
  const effectiveScope: ScopeFilter = !adminMode && scope === 'pending' ? 'all' : scope;

  // Every facet — owner, status, type, activation and the search term — is a
  // query parameter. Filtering in the browser would slice whichever page
  // arrived, so each facet would show an arbitrary fraction of its matches.
  const { data: listData, isFetching: listFetching } = useQuery({
    ...consoleListSubAgentsOptions({
      query: {
        page,
        limit: PAGE_SIZE,
        search: debouncedSearch || undefined,
        ownership: effectiveScope === 'mine' ? 'owned' : effectiveScope === 'shared' ? 'shared' : undefined,
        status: filters.status === 'all' ? undefined : filters.status,
        type: filters.type === 'all' ? undefined : filters.type,
        activated_only: filters.activation === 'enabled' ? true : undefined,
        deactivated_only: filters.activation === 'disabled' ? true : undefined,
      },
    }),
    enabled: effectiveScope !== 'pending',
    placeholderData: keepPreviousData,
  });

  // Approval queue (admin only) — a distinct dataset surfaced via the 'pending' scope
  const { data: pendingData, isFetching: pendingFetching } = useQuery({
    ...listPendingApprovalsApiV1SubAgentsPendingGetOptions({
      query: { page, limit: PAGE_SIZE, search: debouncedSearch || undefined },
    }),
    enabled: effectiveScope === 'pending' && adminMode,
    placeholderData: keepPreviousData,
  });

  const active = effectiveScope === 'pending' ? pendingData : listData;
  const subAgents: SubAgentListItem[] = (active as SubAgentListResponse)?.items ?? [];
  const total = (active as SubAgentListResponse)?.total ?? 0;
  const isFetching = effectiveScope === 'pending' ? pendingFetching : listFetching;

  const getEmptyMessage = (): string => {
    if (debouncedSearch) return 'No sub-agents match your search';
    switch (effectiveScope) {
      case 'mine':
        return "You haven't created any sub-agents yet";
      case 'shared':
        return 'No sub-agents have been shared with you';
      case 'pending':
        return 'No sub-agents are pending approval';
      default:
        return 'No sub-agents found';
    }
  };

  const handleSelectSubAgent = (subAgent: SubAgentListItem) => {
    navigate(`/app/subagents/${subAgent.id}`);
  };

  const handleCreateNew = () => {
    navigate('/app/subagents/new');
  };

  return (
    <div className="flex flex-col gap-6 p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Sub-Agents</h1>
          <p className="text-muted-foreground">
            Create, manage, and test your AI sub-agents
          </p>
        </div>
        <Button onClick={handleCreateNew}>
          <Plus className="mr-2 h-4 w-4" />
          Create Sub-Agent
        </Button>
      </div>

      {/* Content */}
      <SubAgentList
        subAgents={subAgents}
        onSelect={handleSelectSubAgent}
        emptyMessage={getEmptyMessage()}
        showManageAccess
        scope={effectiveScope}
        onScopeChange={handleScopeChange}
        showPendingScope={adminMode}
        filters={filters}
        onFiltersChange={handleFiltersChange}
        total={total}
        isFetching={isFetching}
      />

      <Pagination page={page} limit={PAGE_SIZE} total={total} onPageChange={setPage} />
    </div>
  );
}
