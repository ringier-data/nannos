import { z } from 'zod';
import type { ObjectTypeRegistry } from '@nannos/embed-sdk';
import { SUB_AGENT_NAME_MAX, SUB_AGENT_NAME_RE } from '@/lib/subAgentName';

const FOUNDRY_SCOPES = [
  'api:use-ontologies-read',
  'api:use-ontologies-write',
  'api:use-aip-agents-read',
  'api:use-aip-agents-write',
  'api:use-mediasets-read',
  'api:use-mediasets-write',
] as const;

const THINKING_LEVELS = ['minimal', 'low', 'medium', 'high', 'xhigh'] as const;

// Shared by the binding dialog on an existing sub-agent and the create-embedded form.
const embedBindingSchema = z.object({
  base_url: z
    .string()
    .describe(
      'Origin of the application that publishes the agent definition under /.well-known/agent-skills/: scheme and host only, no path and no trailing slash, e.g. "https://riad.alloy.ch"'
    ),
  azps: z
    .array(z.string().min(1))
    .describe(
      "OAuth client ids (the token's `azp` claim) whose users get this sub-agent activated automatically. At least one is required; each client id can belong to only one embedded agent"
    ),
});

export const subAgentObjectTypes = {
  SubAgent: {
    singular: 'Sub-agent',
    idShape: 'simple-numeric',
    schema: z.object({
      // First, so switching the type (which clears the other types' fields) runs before the rest of a batch.
      type: z
        .enum(['local', 'remote', 'foundry'])
        .describe(
          'Agent type, settable only while creating (fixed afterwards). local = runs in Nannos on a model with a system prompt, tools and skills; remote = an external agent reached over the A2A protocol at agent_url; foundry = a Palantir Foundry query API. Changing it clears the fields of the other types, so set it before the type-specific fields'
        ),
      name: z
        .string()
        .max(SUB_AGENT_NAME_MAX)
        .regex(SUB_AGENT_NAME_RE)
        .describe(
          `Identifier the orchestrator uses for this sub-agent: must start with a letter, then only letters, digits, "-" and "_" (no spaces), at most ${SUB_AGENT_NAME_MAX} characters, e.g. "contract-reviewer"`
        ),
      description: z
        .string()
        .min(1)
        .describe(
          "Required. What tasks this agent handles. The orchestrator reads it to decide when to delegate to this agent, so be specific about its skills and scope. Read-only on embedded (application-published) agents"
        ),
      is_public: z
        .boolean()
        .describe('true = every user can use the sub-agent without a group permission; false = only owner and permitted groups'),
      model: z
        .string()
        .min(1)
        .describe(
          'Local agents only, required. Either a capability tier — "tier:low" (cheaper/faster), "tier:standard" or "tier:premium" (highest capability) — which follows the fleet default model for that tier and survives model upgrades, or a concrete model alias offered in the Model picker (e.g. "claude-sonnet-4.6"). Prefer a tier unless the user names a model. Not settable when the embedding application publishes the model tier'
        ),
      system_prompt: z
        .string()
        .describe(
          "Local agents only, required. The system prompt that defines the agent's behaviour, expertise and output style. Read-only on embedded agents"
        ),
      mcp_tools: z
        .array(z.string())
        .describe(
          "Local agents only. Exact names of the MCP tools the agent may call, as listed in the tool picker. Empty = no MCP tools at all, only the built-in essentials. Not settable when the embedding application publishes the tool list"
        ),
      enable_thinking: z
        .boolean()
        .describe(
          'Local agents only. Extended thinking for complex reasoning; only offered for a concrete model that supports it (not for a tier). Turning it off clears thinking_level; turning it on defaults the level to "low". Not settable when the embedding application publishes the thinking level'
        ),
      thinking_level: z
        .enum(THINKING_LEVELS)
        .nullable()
        .describe(
          'Reasoning effort while enable_thinking is true; null when thinking is off. Set together with enable_thinking. Models support different subsets: a level the chosen model does not offer is reset to "low"'
        ),
      sandbox_enabled: z
        .boolean()
        .describe(
          'Local agents only. Run skill scripts in an isolated sandbox (needs a sandbox provider on the server). Switched on automatically when a skill ships executable files'
        ),
      agent_url: z
        .string()
        .describe('Remote agents only, required. Full A2A endpoint URL including https://, e.g. "https://my-agent.example.com/a2a"'),
      foundry_hostname: z
        .string()
        .describe('Foundry agents only, required. Foundry instance hostname without https://, e.g. "example.palantirfoundry.com"'),
      foundry_client_id: z.string().describe('Foundry agents only, required. OAuth2 client id issued by Foundry'),
      foundry_ontology_rid: z
        .string()
        .describe('Foundry agents only, required. Resource identifier of the ontology, e.g. "ri.ontology.main.ontology.xxx"'),
      foundry_query_api_name: z
        .string()
        .describe('Foundry agents only, required. API name of an existing query defined in that ontology, e.g. "createTicketQuery"'),
      foundry_scopes: z
        .array(z.enum(FOUNDRY_SCOPES))
        .describe(
          'Foundry agents only, at least one required. OAuth2 scopes the agent requests; grant only what its operations need (least privilege)'
        ),
      foundry_version: z
        .string()
        .describe('Foundry agents only, optional. Version of the Foundry query API, e.g. "v1"; empty = unset'),
    }),
    highlightLabels: {
      name: 'Name',
      description: 'Description',
      is_public: 'Public Access',
      model: 'Model',
      system_prompt: 'System Prompt',
      enable_thinking: 'Extended Thinking',
      thinking_level: 'Thinking Level',
      sandbox_enabled: 'Sandbox Execution',
      agent_url: 'Agent URL',
      foundry_hostname: 'Hostname',
      foundry_client_id: 'Client ID',
      foundry_ontology_rid: 'Ontology RID',
      foundry_query_api_name: 'Query API Name',
      foundry_scopes: 'API Scopes',
      foundry_version: 'Version (Optional)',
    },
  },
  // Actions only (no fields): the Version History's per-version menu items.
  SubAgentVersions: {
    singular: 'Version history',
    idShape: 'simple-numeric',
    schema: z.object({}),
    label: ({ id }) => `Version history (sub-agent ${id})`,
  },
  EmbedBinding: {
    singular: 'Embed binding',
    idShape: 'nested',
    parentSingular: 'sub-agent',
    schema: embedBindingSchema,
    highlightLabels: { base_url: 'Authority origin', azps: 'OAuth client ids' },
    // The binding has no id of its own: it is the sub-agent's, and exists or not.
    label: ({ parentId, isExisting }) =>
      isExisting ? `Embed binding (sub-agent ${parentId})` : `New embed binding (sub-agent ${parentId})`,
  },
  EmbeddedAgent: {
    singular: 'Embedded agent',
    idShape: 'simple-numeric',
    schema: embedBindingSchema,
    highlightLabels: { base_url: 'Base URL', azps: 'Allowed OAuth client ids' },
  },
  SubAgentSaveSummary: {
    singular: 'Save summary',
    idShape: 'simple-numeric',
    schema: z.object({
      change_summary: z
        .string()
        .describe(
          'Optional note recorded on the new configuration version this save creates: what changed and why, in a sentence or two, e.g. "Tightened the system prompt and switched to the premium tier"'
        ),
    }),
    highlightLabels: { change_summary: 'Change Summary' },
    label: ({ id }) => `Save summary (sub-agent ${id})`,
  },
  SubAgentApprovalRequest: {
    singular: 'Approval request',
    idShape: 'simple-numeric',
    schema: z.object({
      change_summary: z
        .string()
        .min(1)
        .describe(
          'Required. Tells the reviewer what this draft version changes compared with the approved one, e.g. "Updated system prompt to improve response quality; added the calendar tools"'
        ),
    }),
    highlightLabels: { change_summary: 'Change Summary' },
    label: ({ id }) => `Approval request (sub-agent ${id})`,
  },
  SubAgentRejection: {
    singular: 'Rejection',
    idShape: 'simple-numeric',
    schema: z.object({
      rejection_reason: z
        .string()
        .min(1)
        .describe('Required. Why the reviewer rejects this sub-agent or version, and what the owner should change'),
    }),
    highlightLabels: { rejection_reason: 'Rejection Reason' },
    label: ({ id }) => `Rejection (sub-agent ${id})`,
  },
} satisfies ObjectTypeRegistry;
