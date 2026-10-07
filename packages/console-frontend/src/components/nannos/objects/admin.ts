import { z } from 'zod';
import type { ObjectTypeRegistry } from '@nannos/embed-sdk';

// A number typed into an <input type="number">: the form holds it as text.
const decimalText = z.string().regex(/^(\d+(\.\d*)?|\.\d+)?$/);

export const adminObjectTypes = {
  Group: {
    singular: 'Group',
    idShape: 'simple-numeric',
    schema: z.object({
      name: z.string().min(1).describe('Group name, shown in group lists and member pickers'),
      description: z.string().describe('What the group is for (optional; empty clears it)'),
    }),
    highlightLabels: { name: 'Name', description: 'Description' },
  },
  GroupMembers: {
    singular: 'Group members',
    idShape: 'nested',
    parentSingular: 'group',
    // People are picked by email from the dialog's list (search_users narrows it); ids stay opaque.
    schema: z.object({
      members: z
        .array(z.string().email())
        .describe(
          "Emails of the users to add. Each must appear in the dialog's user list (invoke search_users " +
            'first); replaces the current selection',
        ),
      role: z
        .enum(['read', 'write', 'manager'])
        .describe(
          'Group role given to every user added in this dialog: read, write or manager access to group ' +
            'resources (managers can also manage the group)',
        ),
    }),
    highlightLabels: { role: 'Role', members: 'Users' },
  },
  GroupServerAccess: {
    singular: 'Server access grant',
    idShape: 'nested',
    parentSingular: 'group',
    schema: z.object({
      server_slug: z
        .string()
        .describe(
          'Name of the MCP gateway server to grant this group access to. Must be one of the servers offered ' +
            'in the Server dropdown (gateway servers the group does not have access to yet)',
        ),
      role: z
        .enum(['member', 'maintainer', 'admin'])
        .describe("The group's role on that gateway server: member (use its tools), maintainer, or admin"),
    }),
    highlightLabels: { server_slug: 'Server', role: 'Role' },
  },
  GatewayModel: {
    singular: 'Gateway model',
    idShape: 'simple-string',
    schema: z.object({
      mode: z
        .enum(['chat', 'embedding'])
        .describe(
          'chat for conversational models, embedding for vector-embedding models. Embedding models bill input ' +
            'only and have no thinking or web search',
        ),
      litellm_model: z
        .string()
        .describe(
          'Gateway (LiteLLM) model id, prefixed with its provider route, e.g. "bedrock/eu.anthropic.claude-sonnet-4-6" ' +
            'or "vertex_ai/gemini-embedding-2". An id from the gateway catalog pre-fills the alias (when registering ' +
            'and not yet set), input modes, capabilities and prices, exactly like picking it from the list — set ' +
            'those fields after this one to override them',
        ),
      model_name: z
        .string()
        .describe(
          'Alias apps request the model by, e.g. "claude-sonnet-4-6"; must not already be registered. ' +
            'Fixed once registered: ignored when editing an existing model',
        ),
      base_model: z
        .string()
        .describe(
          'Azure only: the catalog model (and pricing tier) this deployment serves, e.g. "azure/eu/gpt-6-sol"; ' +
            'used for max-tokens and cost metadata. Ignored for other providers',
        ),
      aws_region_name: z
        .string()
        .describe(
          "Bedrock only: AWS region to call the model in, e.g. \"us-east-1\". Empty uses the gateway's own region; " +
            'Bedrock availability is per region. Ignored for other providers',
        ),
      vertex_location: z
        .string()
        .describe(
          'Vertex AI only: serving region (not the GCP project), e.g. "europe-west4". Empty uses the deployment ' +
            'default. Ignored for other providers',
        ),
      vertex_project: z
        .string()
        .describe("Vertex AI only: GCP project id. Empty uses the proxy's default project. Ignored for other providers"),
      input_modes: z
        .array(z.enum(['text', 'image', 'audio', 'video', 'file']))
        .describe(
          'Input types the model accepts (chat models). For embedding models, "image" makes it eligible as the ' +
            'multimodal embedding default',
        ),
      supports_reasoning: z
        .boolean()
        .describe('Chat only: the model can think (unlocks the reasoning-effort picker). Saved explicitly, overriding the catalog'),
      supports_web_search: z
        .boolean()
        .describe(
          'Chat only: the model can do grounded web search, making it eligible to back the console_web_search tool ' +
            '(then also set the web_search price). Saved explicitly, overriding the catalog',
        ),
      prices: z
        .record(z.string(), decimalText)
        .describe(
          'USD per million units, keyed by billing unit, as decimal text: base_input_tokens, base_output_tokens ' +
            '(chat only), cache_read_input_tokens, cache_creation_input_tokens, input_images (embedding only, per ' +
            'million images), web_search (chat with web search, per million searches). Replaces the whole map — ' +
            'include every unit to keep. Units not shown for the mode are dropped on save; at least one price > 0 ' +
            'is required. Saving writes a new rate-card version',
        ),
    }),
    highlightLabels: {
      mode: 'Mode',
      litellm_model: 'Gateway model id',
      model_name: 'Alias',
      base_model: 'Base model',
      aws_region_name: 'AWS region',
      vertex_location: 'Vertex location',
      vertex_project: 'Vertex project',
      input_modes: 'Input modes',
      supports_reasoning: 'Capabilities',
      supports_web_search: 'Capabilities',
      prices: 'Pricing',
    },
  },
  ToolRiskScore: {
    singular: 'Tool risk score',
    idShape: 'simple-string',
    schema: z.object({
      tool_name: z
        .string()
        .min(1)
        .describe('Exact MCP tool name, e.g. "console_create_skill". Fixed once created: ignored when editing'),
      server_slug: z
        .string()
        .describe(
          'MCP server the tool belongs to, e.g. "console" or "github"; "_self" for in-process tools only. Empty ' +
            'means "console". Fixed once created: ignored when editing',
        ),
      base_score: z
        .number()
        .min(0)
        .max(1)
        .describe('Baseline risk 0–1 (slider steps of 0.05): 1.0 always interrupts for approval, 0.0 never does'),
      allowed_actions: z
        .array(z.enum(['approve', 'edit', 'reject']))
        .describe('What the user may do when this tool call is interrupted for review'),
      risk_factors_json: z
        .string()
        .refine((text) => {
          try {
            return typeof JSON.parse(text) === 'object';
          } catch {
            return false;
          }
        }, 'Must be a JSON object')
        .describe(
          'JSON object (as text) mapping parameter names to risk profiles, each ' +
            '{"risky_values": {<glob>: <score 0–1>}, "default_contribution": <0–1>}, e.g. ' +
            '{"method": {"risky_values": {"DELETE*": 0.95}, "default_contribution": 0.1}}. "{}" for none',
        ),
    }),
    highlightLabels: {
      tool_name: 'Tool Name',
      server_slug: 'Server Slug',
      base_score: 'Base Score',
      allowed_actions: 'Allowed Actions',
      risk_factors_json: 'Risk Factors',
    },
  },
  ModelPricing: {
    singular: 'Model pricing',
    idShape: 'simple-string',
    schema: z.object({
      provider: z
        .string()
        .describe(
          'Runtime provider family the gateway reports, e.g. "bedrock", "vertex_ai", "azure" — not a catalog tag ' +
            '("bedrock_converse") or a region ("eu"); the card only bills when it matches. Fixed once created',
        ),
      model_name: z
        .string()
        .describe('Model name usage is reported under, e.g. "claude-sonnet-4-20250514". Fixed once created'),
      model_name_pattern: z
        .string()
        .describe(
          'Optional regex matching model variants, e.g. "^gpt-4o-mini(-\\d{4}-\\d{2}-\\d{2})?$"; empty for an exact ' +
            'match on model_name only. Fixed once created',
        ),
      input_price: decimalText.describe(
        'USD per million base input tokens, as decimal text; empty adds no base input rate. Editing writes a new rate version',
      ),
      output_price: decimalText.describe(
        'USD per million base output tokens, as decimal text; empty adds no base output rate. Editing writes a new rate version',
      ),
    }),
    highlightLabels: {
      provider: 'Provider',
      model_name: 'Model Name',
      model_name_pattern: 'Model Name Pattern',
      input_price: 'Base Input Price',
      output_price: 'Base Output Price',
    },
  },
  BudgetGuard: {
    singular: 'Budget guard',
    idShape: 'simple-string',
    label: () => 'Budget guard settings',
    schema: z.object({
      enabled: z
        .boolean()
        .describe('Enforce the limit: when off, spend is still tracked but LLM requests are never blocked'),
      limit: decimalText.describe(
        'Global LLM spend cap in USD per calendar month, as positive decimal text; at the cap new requests are ' +
          'rejected until next month or a higher limit',
      ),
      thresholds: z
        .string()
        .describe('Comma-separated percentages of the limit (1–100) at which to warn, e.g. "80, 90, 95"'),
    }),
    highlightLabels: { enabled: 'Enforcement enabled', limit: 'Monthly limit', thresholds: 'Warning thresholds' },
  },
  ScimToken: {
    singular: 'SCIM token',
    idShape: 'simple-string',
    schema: z.object({
      name: z.string().min(1).describe('Token name, e.g. the identity provider using it ("Azure AD SCIM")'),
      description: z.string().describe('What the token is used for (optional)'),
      expiresAt: z
        .string()
        .regex(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})?$/)
        .describe("Expiry as local date-time \"YYYY-MM-DDTHH:mm\" in the user's time zone; empty never expires"),
    }),
    highlightLabels: { name: 'Name', description: 'Description', expiresAt: 'Expires at' },
  },
  BrokerClient: {
    singular: 'Broker client',
    idShape: 'simple-numeric',
    schema: z.object({
      clientId: z
        .string()
        .describe(
          'Keycloak client id of the application, e.g. "cockpit-embed"; it calls the broker with its own ' +
            'client-credentials token. Fixed once registered: ignored when editing',
        ),
      name: z.string().min(1).describe('Display name of the application, e.g. "Cockpit"'),
      description: z.string().describe('Optional description; empty clears it'),
      redirectUris: z
        .string()
        .describe(
          'Allowed sign-in redirect URIs, one per line (at least one). Exact match; only the first host label may ' +
            'contain "*", for preview environments',
        ),
      enabled: z
        .boolean()
        .describe("Off stops the client's sign-ins and tokens but keeps its users signed in for when it is on again"),
      requireBindingSecret: z
        .boolean()
        .describe(
          'Token requests must carry the secret the client received at sign-in. Turn on only once the client ' +
            'sends it: users who signed in through it before must sign in again',
        ),
    }),
    highlightLabels: {
      clientId: 'Client ID',
      name: 'Name',
      description: 'Description',
      redirectUris: 'Redirect URIs',
      enabled: 'Enabled',
      requireBindingSecret: 'Require binding secret',
    },
  },
  // The bearer token is deliberately absent: credentials never go through the assistant.
  OutboundScimEndpoint: {
    singular: 'Outbound SCIM endpoint',
    idShape: 'simple-numeric',
    schema: z.object({
      name: z.string().min(1).describe('Endpoint name, e.g. "Salesforce SCIM"'),
      endpointUrl: z.string().min(1).describe('SCIM 2.0 base URL of the external server, e.g. "https://api.example.com/scim/v2"'),
      pushUsers: z.boolean().describe('Provision user changes to this endpoint'),
      pushGroups: z.boolean().describe('Provision group changes to this endpoint'),
      isMcpGateway: z
        .boolean()
        .describe('This endpoint is the MCP gateway (Gatana): enables server access management for groups synced to it'),
      enabled: z
        .boolean()
        .optional()
        .describe('Editing only: whether provisioning to this endpoint is active (new endpoints start enabled)'),
    }),
    highlightLabels: {
      name: 'Name',
      endpointUrl: 'SCIM Base URL',
      pushUsers: 'Push Users',
      pushGroups: 'Push Groups',
      isMcpGateway: 'MCP Gateway endpoint',
      enabled: 'Enabled',
    },
  },
} satisfies ObjectTypeRegistry;
