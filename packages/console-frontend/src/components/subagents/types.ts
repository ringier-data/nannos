// Re-export types from generated SDK for consistency
export type {
  SubAgent,
  SubAgentListItem,
  SubAgentType,
  SubAgentStatus,
  SubAgentOwner,
  SubAgentConfigVersion,
  SubAgentCreate,
  SubAgentUpdate,
  SubAgentApproval,
  SubAgentPermissionsUpdate,
  SubAgentListResponse,
  FoundryScope,
  SkillDefinition,
  SkillFile,
} from '@/api/generated/types.gen';

// Local type aliases for backward compatibility
export interface RemoteAgentConfiguration {
  agent_url: string;
}

export interface LocalAgentConfiguration {
  system_prompt: string;
  mcp_tools?: string[];
  enable_thinking?: boolean;
  thinking_level?: string | null;
  skills?: Array<{ name: string; description: string; body: string; files?: Array<{ path: string; content: string }>; source?: string | null; source_hash?: string | null }>;
  sandbox_enabled?: boolean;
}

export interface FoundryAgentConfiguration {
  foundry_hostname: string;
  foundry_client_id: string;
  foundry_client_secret?: string;
  foundry_client_secret_ref?: number | null;
  foundry_ontology_rid: string;
  foundry_query_api_name: string;
  foundry_scopes: string[];
  foundry_version?: string;
}

export type SubAgentConfiguration = RemoteAgentConfiguration | LocalAgentConfiguration | FoundryAgentConfiguration;

export interface UserGroup {
  id: number;
  name: string;
  description: string | null;
  permissions: Record<string, string[]>;
}

export interface SubAgentPermission {
  id: number;
  sub_agent_id: number;
  user_group_id: number;
}

// Form types for creating/updating sub-agents
export interface SubAgentFormData {
  name: string;
  description: string;
  model?: string;  // LLM model to use (e.g., 'gpt-4', 'claude-3-opus')
  type: 'remote' | 'local' | 'foundry' | 'automated';
  is_public?: boolean;  // If true, accessible to all users without group permissions
  configuration: SubAgentConfiguration;
  mcp_tools?: string[];  // MCP tool names for local agents
  skills?: Array<{ name: string; description: string; body: string; files?: Array<{ path: string; content: string }>; source?: string | null; source_hash?: string | null; sandbox_required?: boolean }>;
  sandbox_enabled?: boolean;
}

export interface SubAgentApprovalData {
  action: 'approve' | 'reject';
  rejection_reason?: string;
}

export interface SubAgentAssignmentData {
  user_group_ids: number[];
}

// Helper type guards
export function isRemoteConfiguration(config: { [key: string]: unknown }): boolean {
  return 'agent_url' in config;
}

export function isLocalConfiguration(config: { [key: string]: unknown }): boolean {
  return 'system_prompt' in config;
}

export function isFoundryConfiguration(config: { [key: string]: unknown }): boolean {
  return 'foundry_hostname' in config && 'foundry_client_id' in config;
}
