import { Search, Filter } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { SubAgentCard } from './SubAgentCard';
import {
  EMPTY_SUB_AGENT_FILTERS,
  type ScopeFilter,
  type SubAgentFilters,
  type SubAgentListItem,
  type SubAgentStatus,
  type SubAgentType,
} from './types';

export type { ScopeFilter, SubAgentFilters };

interface SubAgentListProps {
  subAgents: SubAgentListItem[];
  onSelect: (subAgent: SubAgentListItem) => void;
  emptyMessage?: string;
  showManageAccess?: boolean;
  scope?: ScopeFilter;
  onScopeChange?: (scope: ScopeFilter) => void;
  showPendingScope?: boolean;
  /** Every facet is a server query parameter, so the page owns the state. */
  filters: SubAgentFilters;
  onFiltersChange: (filters: SubAgentFilters) => void;
  /** Matches on the server, which exceeds the rows on this page. */
  total: number;
  /**
   * Which facets the current dataset can actually honour. A facet the endpoint
   * has no parameter for is hidden rather than rendered inert — a dropdown that
   * silently does nothing is worse than an absent one. Defaults to all of them.
   */
  availableFacets?: readonly ('search' | 'status' | 'type' | 'activation')[];
  isFetching?: boolean;
}

export function SubAgentList({
  subAgents,
  onSelect,
  emptyMessage = 'No sub-agents found',
  showManageAccess = false,
  scope = 'all',
  onScopeChange,
  showPendingScope = false,
  filters,
  onFiltersChange,
  total,
  isFetching = false,
  availableFacets = ['search', 'status', 'type', 'activation'],
}: SubAgentListProps) {
  const shows = (facet: 'search' | 'status' | 'type' | 'activation') =>
    availableFacets.includes(facet);
  // The server already applied every facet; these rows are the page as-is.
  const filteredSubAgents = subAgents;

  const { search: searchQuery, status: statusFilter, type: typeFilter, activation: activationFilter } =
    filters;

  const setSearchQuery = (search: string) => onFiltersChange({ ...filters, search });
  const setStatusFilter = (status: SubAgentStatus | 'all') =>
    onFiltersChange({ ...filters, status });
  const setTypeFilter = (type: SubAgentType | 'all') => onFiltersChange({ ...filters, type });
  const setActivationFilter = (activation: 'all' | 'enabled' | 'disabled') =>
    onFiltersChange({ ...filters, activation });

  const hasFilters =
    searchQuery !== '' || statusFilter !== 'all' || typeFilter !== 'all' || activationFilter !== 'all';

  const clearFilters = () => onFiltersChange(EMPTY_SUB_AGENT_FILTERS);

  return (
    <div className={`flex flex-col gap-4 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        {onScopeChange && (
          <Select value={scope} onValueChange={(v) => onScopeChange(v as ScopeFilter)}>
            <SelectTrigger className="w-[170px]">
              <SelectValue placeholder="Owner" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All owners</SelectItem>
              <SelectItem value="mine">Mine</SelectItem>
              <SelectItem value="shared">Shared with me</SelectItem>
              {showPendingScope && <SelectItem value="pending">Pending approval</SelectItem>}
            </SelectContent>
          </Select>
        )}
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search sub-agents..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-9"
          />
        </div>
        {shows('status') && (
        <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as SubAgentStatus | 'all')}>
          <SelectTrigger className="w-[160px]">
            <Filter className="mr-2 h-4 w-4" />
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Statuses</SelectItem>
            <SelectItem value="draft">Draft</SelectItem>
            <SelectItem value="pending_approval">Pending Approval</SelectItem>
            <SelectItem value="approved">Approved</SelectItem>
            <SelectItem value="rejected">Rejected</SelectItem>
          </SelectContent>
        </Select>
        )}
        {shows('type') && (
        <Select value={typeFilter} onValueChange={(v) => setTypeFilter(v as SubAgentType | 'all')}>
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="Type" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Types</SelectItem>
            <SelectItem value="remote">Remote</SelectItem>
            <SelectItem value="local">Local</SelectItem>
          </SelectContent>
        </Select>
        )}
        {shows('activation') && (
        <Select value={activationFilter} onValueChange={(v) => setActivationFilter(v as 'all' | 'enabled' | 'disabled')}>
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder="Activation" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Activation</SelectItem>
            <SelectItem value="enabled">Enabled</SelectItem>
            <SelectItem value="disabled">Disabled</SelectItem>
          </SelectContent>
        </Select>
        )}
        {hasFilters && (
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            Clear filters
          </Button>
        )}
      </div>

      {/* Results count — `total` is what the filters match on the server, which
          is more than this page holds. */}
      <div className="text-sm text-muted-foreground">
        {total} sub-agent{total !== 1 ? 's' : ''}
        {hasFilters && ' matching'}
        {total > filteredSubAgents.length && ` — showing ${filteredSubAgents.length}`}
      </div>

      {/* List */}
      {filteredSubAgents.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-12">
          <p className="text-muted-foreground">{hasFilters ? 'No matching sub-agents' : emptyMessage}</p>
          {hasFilters && (
            <Button variant="link" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          )}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filteredSubAgents.map((subAgent) => (
            <SubAgentCard
              key={subAgent.id}
              subAgent={subAgent}
              onClick={() => onSelect(subAgent)}
              showManageAccess={showManageAccess}
            />
          ))}
        </div>
      )}
    </div>
  );
}
