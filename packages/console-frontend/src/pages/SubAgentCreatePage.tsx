import { useState } from 'react';
import { useNavigate } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { SubAgentForm } from '@/components/subagents/SubAgentForm';
import { AgentTypeCards, type AgentTypeChoice } from '@/components/subagents/AgentTypeCards';
import { EmbeddedAgentForm } from '@/components/subagents/EmbeddedAgentForm';
import type { SubAgentFormData } from '@/components/subagents/types';
import { consoleCreateSubAgentMutation } from '@/api/generated/@tanstack/react-query.gen';
import type { SubAgent } from '@/api/generated/types.gen';
import { useAuth } from '@/contexts/AuthContext';
import { getErrorMessage } from '@/lib/utils';

export function SubAgentCreatePage() {
  const navigate = useNavigate();
  const { user, adminMode } = useAuth();

  // An embedded agent is created from an application's published definition instead of
  // from these fields, so picking it swaps the whole form out (ADR-0006). Admins only.
  const canCreateEmbedded = adminMode && (user?.is_administrator ?? false);
  const [choice, setChoice] = useState<AgentTypeChoice>('local');

  const createMutation = useMutation({
    ...consoleCreateSubAgentMutation(),
    onSuccess: (data: SubAgent) => {
      toast.success('Sub-agent created successfully');
      // Navigate to the new sub-agent detail page
      navigate(`/app/subagents/${data.id}`);
    },
    onError: (err) => {
      toast.error('Failed to create sub-agent', {
        description: getErrorMessage(err),
      });
    },
  });

  const handleSubmit = async (data: SubAgentFormData) => {
    // Extract configuration fields based on agent type
    const config = data.configuration;

    createMutation.mutate({
      body: {
        name: data.name,
        description: data.description,
        model: data.model as any, // Cast to ModelEnum - validated by form
        model_tier: data.model_tier,
        type: data.type,
        is_public: data.is_public,
        system_prompt: 'system_prompt' in config ? config.system_prompt : undefined,
        agent_url: 'agent_url' in config ? config.agent_url : undefined,
        mcp_tools: 'mcp_tools' in config ? config.mcp_tools : undefined,
        foundry_hostname: 'foundry_hostname' in config ? config.foundry_hostname : undefined,
        foundry_client_id: 'foundry_client_id' in config ? config.foundry_client_id : undefined,
        foundry_client_secret_ref: 'foundry_client_secret_ref' in config ? config.foundry_client_secret_ref : undefined,
        foundry_ontology_rid: 'foundry_ontology_rid' in config ? config.foundry_ontology_rid : undefined,
        foundry_query_api_name: 'foundry_query_api_name' in config ? config.foundry_query_api_name : undefined,
        foundry_scopes: 'foundry_scopes' in config ? config.foundry_scopes as any : undefined,
        foundry_version: 'foundry_version' in config ? config.foundry_version : undefined,
        skills: data.skills,
        sandbox_enabled: data.sandbox_enabled,
      },
    });
  };

  const handleCancel = () => {
    navigate('/app/subagents');
  };

  return (
    <div className="flex flex-col gap-6 p-4 pb-8">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={handleCancel}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div>
          <h1 className="text-2xl font-bold">Create Sub-Agent</h1>
          <p className="text-muted-foreground">
            Configure a new AI sub-agent for your workflows
          </p>
        </div>
      </div>
      {canCreateEmbedded && choice === 'embedded' ? (
        <div className="space-y-6">
          <div>
            <h3 className="text-lg font-semibold mb-4">Choose Agent Type</h3>
            <AgentTypeCards value={choice} onChange={setChoice} showEmbedded />
          </div>
          <EmbeddedAgentForm
            onCancel={handleCancel}
            onCreated={(subAgentId) => navigate(`/app/subagents/${subAgentId}`)}
          />
        </div>
      ) : (
        <SubAgentForm
          onSubmit={handleSubmit}
          onCancel={handleCancel}
          isSubmitting={createMutation.isPending}
          initialType={choice === 'embedded' ? undefined : choice}
          showEmbeddedType={canCreateEmbedded}
          onSelectEmbeddedType={() => setChoice('embedded')}
        />
      )}
    </div>
  );
}
