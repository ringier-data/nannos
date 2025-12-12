import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router';
import { Bot, ExternalLink, Search } from 'lucide-react';
import { toast } from 'sonner';
import {
  playgroundListSubAgentsOptions,
  activateSubAgentApiV1SubAgentsSubAgentIdActivatePostMutation,
  deactivateSubAgentApiV1SubAgentsSubAgentIdDeactivatePostMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { SubAgent, SubAgentListResponse } from '@/api/generated/types.gen';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

type FilterTab = 'all' | 'enabled' | 'available';

export function SubAgentActivationList() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<FilterTab>('all');
  const [searchQuery, setSearchQuery] = useState('');

  const { data: subAgentsData, isLoading } = useQuery({
    ...playgroundListSubAgentsOptions({}),
  });

  const activateMutation = useMutation({
    ...activateSubAgentApiV1SubAgentsSubAgentIdActivatePostMutation(),
    onSuccess: () => {
      toast.success('Sub-agent activated');
      queryClient.invalidateQueries({
        queryKey: playgroundListSubAgentsOptions({}).queryKey,
      });
    },
    onError: () => {
      toast.error('Failed to activate sub-agent');
    },
  });

  const deactivateMutation = useMutation({
    ...deactivateSubAgentApiV1SubAgentsSubAgentIdDeactivatePostMutation(),
    onSuccess: () => {
      toast.success('Sub-agent deactivated');
      queryClient.invalidateQueries({
        queryKey: playgroundListSubAgentsOptions({}).queryKey,
      });
    },
    onError: () => {
      toast.error('Failed to deactivate sub-agent');
    },
  });

  const subAgents = (subAgentsData as SubAgentListResponse)?.items ?? [];

  // Set default tab to 'enabled' if there are activated sub-agents
  useEffect(() => {
    if (subAgents.length > 0) {
      const hasActivated = subAgents.some(sa => sa.is_activated);
      if (hasActivated) {
        setActiveTab('enabled');
      }
    }
  }, [subAgents]);

  const handleToggle = (subAgent: SubAgent) => {
    if (!subAgent.default_version) {
      return; // Cannot activate non-approved sub-agents
    }

    if (subAgent.is_activated) {
      deactivateMutation.mutate({
        path: { sub_agent_id: subAgent.id },
      });
    } else {
      activateMutation.mutate({
        path: { sub_agent_id: subAgent.id },
      });
    }
  };

  const toggleSelectAll = () => {
    const allFilteredActivated = filteredSubAgents.every((sa) => sa.is_activated);
    
    if (allFilteredActivated) {
      // Deactivate all filtered sub-agents
      filteredSubAgents.forEach((sa) => {
        if (sa.is_activated) {
          deactivateMutation.mutate({
            path: { sub_agent_id: sa.id },
          });
        }
      });
    } else {
      // Activate all approved filtered sub-agents
      filteredSubAgents.forEach((sa) => {
        if (sa.default_version && !sa.is_activated) {
          activateMutation.mutate({
            path: { sub_agent_id: sa.id },
          });
        }
      });
    }
  };

  const getFilteredSubAgents = (): SubAgent[] => {
    let result = subAgents;

    // Apply tab filter
    switch (activeTab) {
      case 'enabled':
        result = result.filter((sa) => sa.is_activated);
        break;
      case 'available':
        result = result.filter((sa) => !sa.is_activated && sa.default_version);
        break;
      default:
        // all - no filter
        break;
    }

    // Apply search filter
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      result = result.filter(
        (sa) =>
          sa.name.toLowerCase().includes(query) ||
          (sa.config_version?.description?.toLowerCase() || '').includes(query)
      );
    }

    return result;
  };

  const filteredSubAgents = getFilteredSubAgents();
  const activatedCount = subAgents.filter((sa) => sa.is_activated).length;
  const availableCount = subAgents.filter((sa) => !sa.is_activated && sa.default_version).length;

  const isApproved = (subAgent: SubAgent) => subAgent.default_version !== null;
  const allSelected = filteredSubAgents.length > 0 && filteredSubAgents.every((sa) => sa.is_activated);
  const someSelected = filteredSubAgents.some((sa) => sa.is_activated) && !allSelected;

  return (
    <div className="space-y-4">
      {/* Header with tabs */}
      <div className="flex gap-2">
        <Button
          variant={activeTab === 'all' ? 'default' : 'outline'}
          size="sm"
          onClick={() => setActiveTab('all')}
        >
          All ({subAgents.length})
        </Button>
        <Button
          variant={activeTab === 'enabled' ? 'default' : 'outline'}
          size="sm"
          onClick={() => setActiveTab('enabled')}
        >
          Enabled ({activatedCount})
        </Button>
        <Button
          variant={activeTab === 'available' ? 'default' : 'outline'}
          size="sm"
          onClick={() => setActiveTab('available')}
        >
          Available ({availableCount})
        </Button>
      </div>

      {/* Search input */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Search sub-agents by name or description..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="pl-10"
        />
      </div>

      {isLoading ? (
        <div className="text-center py-8 text-muted-foreground">Loading...</div>
      ) : filteredSubAgents.length === 0 ? (
        <div className="text-center py-12 border rounded-lg">
          <Bot className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
          <p className="text-muted-foreground">
            {searchQuery ? 'No sub-agents match your search' : 'No sub-agents available'}
          </p>
          {searchQuery && (
            <Button variant="link" onClick={() => setSearchQuery('')} className="mt-2">
              Clear search
            </Button>
          )}
        </div>
      ) : (
        <div className="border rounded-lg">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-12">
                  <Checkbox
                    checked={allSelected}
                    onCheckedChange={toggleSelectAll}
                    aria-label="Toggle all"
                    ref={(el) => {
                      if (el) {
                        (el as any).indeterminate = someSelected && !allSelected;
                      }
                    }}
                  />
                </TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Description</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredSubAgents.map((subAgent) => {
                const approved = isApproved(subAgent);
                const canToggle = approved;

                return (
                  <TableRow key={subAgent.id}>
                    <TableCell>
                      <Checkbox
                        checked={subAgent.is_activated ?? false}
                        onCheckedChange={() => handleToggle(subAgent)}
                        aria-label={`Activate ${subAgent.name}`}
                        disabled={!canToggle || activateMutation.isPending || deactivateMutation.isPending}
                      />
                    </TableCell>
                    <TableCell>
                      <Link
                        to={`/app/subagents/${subAgent.id}`}
                        className="flex items-center gap-2 font-medium hover:underline"
                      >
                        <Bot className="h-4 w-4" />
                        {subAgent.name}
                        <ExternalLink className="h-3 w-3 opacity-50" />
                      </Link>
                    </TableCell>
                    <TableCell className="text-muted-foreground max-w-md truncate">
                      {subAgent.config_version?.description || '-'}
                    </TableCell>
                    <TableCell>
                      {approved ? (
                        <Badge variant="default">Approved</Badge>
                      ) : (
                        <Badge variant="secondary">Pending</Badge>
                      )}
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
