import { useState } from 'react';
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
import type { SubAgent, SubAgentStatus, SubAgentType } from './types';

interface SubAgentListProps {
  subAgents: SubAgent[];
  onSelect: (subAgent: SubAgent) => void;
  emptyMessage?: string;
  showManageAccess?: boolean;
}

export function SubAgentList({ subAgents, onSelect, emptyMessage = 'No sub-agents found', showManageAccess = false }: SubAgentListProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<SubAgentStatus | 'all'>('all');
  const [typeFilter, setTypeFilter] = useState<SubAgentType | 'all'>('all');

  const filteredSubAgents = subAgents.filter((sa) => {
    const matchesSearch =
      searchQuery === '' ||
      sa.name.toLowerCase().includes(searchQuery.toLowerCase());

    const matchesStatus = statusFilter === 'all';
    const matchesType = typeFilter === 'all' || sa.type === typeFilter;

    return matchesSearch && matchesStatus && matchesType;
  });

  const hasFilters = searchQuery !== '' || statusFilter !== 'all' || typeFilter !== 'all';

  const clearFilters = () => {
    setSearchQuery('');
    setStatusFilter('all');
    setTypeFilter('all');
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search sub-agents..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-9"
          />
        </div>
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
        {hasFilters && (
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            Clear filters
          </Button>
        )}
      </div>

      {/* Results count */}
      <div className="text-sm text-muted-foreground">
        {filteredSubAgents.length} sub-agent{filteredSubAgents.length !== 1 ? 's' : ''}
        {hasFilters && ` (filtered from ${subAgents.length})`}
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
