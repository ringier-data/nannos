import { useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { SubAgentList, type ScopeFilter } from '@/components/subagents/SubAgentList';
import {
  consoleListSubAgentsOptions,
  listPendingApprovalsApiV1SubAgentsPendingGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { SubAgentListItem, SubAgentListResponse } from '@/api/generated/types.gen';
import { useAuth } from '@/contexts/AuthContext';

export function SubAgentsPage() {
  const navigate = useNavigate();
  const [scope, setScope] = useState<ScopeFilter>('all');
  const { user, adminMode } = useAuth();

  // Derive the effective scope so the approval queue isn't shown once admin mode is off,
  // without resetting state in an effect.
  const effectiveScope: ScopeFilter = !adminMode && scope === 'pending' ? 'all' : scope;

  // Full list (owned + shared) — owner faceting happens client-side in SubAgentList
  const { data: listData } = useQuery({
    ...consoleListSubAgentsOptions({}),
    enabled: effectiveScope !== 'pending',
  });

  // Approval queue (admin only) — a distinct dataset surfaced via the 'pending' scope
  const { data: pendingData } = useQuery({
    ...listPendingApprovalsApiV1SubAgentsPendingGetOptions(),
    enabled: effectiveScope === 'pending' && adminMode,
  });

  const subAgents: SubAgentListItem[] =
    effectiveScope === 'pending'
      ? (pendingData as SubAgentListResponse)?.items ?? []
      : (listData as SubAgentListResponse)?.items ?? [];

  const getEmptyMessage = (): string => {
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
        currentUserId={user?.id}
        scope={effectiveScope}
        onScopeChange={setScope}
        showPendingScope={adminMode}
      />
    </div>
  );
}
