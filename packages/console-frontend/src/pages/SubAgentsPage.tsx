import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router';
import { Plus, Bot, Users, Clock } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { SubAgentList } from '@/components/subagents/SubAgentList';
import {
  consoleListSubAgentsOptions,
  listPendingApprovalsApiV1SubAgentsPendingGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { SubAgent, SubAgentListResponse } from '@/api/generated/types.gen';
import { useAuth } from '@/contexts/AuthContext';

type TabId = 'my' | 'accessible' | 'pending';

interface Tab {
  id: TabId;
  label: string;
  icon: typeof Bot;
  requiresAdmin?: boolean;
}

const tabs: Tab[] = [
  { id: 'my', label: 'My Sub-Agents', icon: Bot },
  { id: 'accessible', label: 'Accessible', icon: Users },
  { id: 'pending', label: 'Pending Approval', icon: Clock, requiresAdmin: true },
];

export function SubAgentsPage() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<TabId>('my');
  const { user, adminMode } = useAuth();

  // Reset to 'my' tab when admin mode is disabled and currently on pending tab
  useEffect(() => {
    if (!adminMode && activeTab === 'pending') {
      setActiveTab('my');
    }
  }, [adminMode, activeTab]);

  // Fetch owned sub-agents
  const { data: ownedData } = useQuery({
    ...consoleListSubAgentsOptions({
      query: { owned_only: true },
    }),
    enabled: activeTab === 'my',
  });

  // Fetch all accessible sub-agents (for 'accessible' tab)
  const { data: accessibleData } = useQuery({
    ...consoleListSubAgentsOptions({}),
    enabled: activeTab === 'accessible',
  });

  // Fetch pending approvals (admin mode only)
  const { data: pendingData } = useQuery({
    ...listPendingApprovalsApiV1SubAgentsPendingGetOptions(),
    enabled: activeTab === 'pending' && adminMode,
  });

  // Show pending tab only when admin mode is active
  const visibleTabs = tabs.filter(
    (tab) => !tab.requiresAdmin || adminMode
  );

  const getSubAgentsForTab = (): SubAgent[] => {
    switch (activeTab) {
      case 'my':
        return (ownedData as SubAgentListResponse)?.items ?? [];
      case 'accessible':
        // Filter out owned sub-agents from accessible list
        return ((accessibleData as SubAgentListResponse)?.items ?? []).filter((sa: any) => sa.owner_user_id !== user?.id);
      case 'pending':
        return (pendingData as SubAgentListResponse)?.items ?? [];
      default:
        return [];
    }
  };

  const getEmptyMessage = (): string => {
    switch (activeTab) {
      case 'my':
        return "You haven't created any sub-agents yet";
      case 'accessible':
        return 'No sub-agents have been shared with you';
      case 'pending':
        return 'No sub-agents are pending approval';
      default:
        return 'No sub-agents found';
    }
  };

  const handleSelectSubAgent = (subAgent: SubAgent) => {
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

      {/* Tabs */}
      <div className="flex gap-1 border-b">
        {visibleTabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === tab.id
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground hover:border-muted-foreground/50'
            }`}
          >
            <tab.icon className="h-4 w-4" />
            {tab.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <SubAgentList
        subAgents={getSubAgentsForTab()}
        onSelect={handleSelectSubAgent}
        emptyMessage={getEmptyMessage()}
        showManageAccess={activeTab === 'my' || adminMode}
      />
    </div>
  );
}
