import { z } from 'zod';
import type { ObjectTypeRegistry } from '@nannos/embed-sdk';

/** The zones the Timezone dropdown offers (SettingsPage adds UTC, which Chrome's list lacks). */
const TIMEZONES: ReadonlySet<string> = (() => {
  try {
    return new Set(['UTC', ...Intl.supportedValuesOf('timeZone')]);
  } catch {
    return new Set(['UTC', 'Europe/Zurich', 'America/New_York', 'America/Los_Angeles', 'Europe/London', 'Europe/Berlin', 'Asia/Tokyo']);
  }
})();

export const settingsObjectTypes = {
  // The signed-in user's own settings: always an update of id 'me'.
  Settings: {
    singular: 'Settings',
    idShape: 'simple-string',
    schema: z.object({
      preferredModel: z
        .string()
        .min(1)
        .nullable()
        .describe(
          'Model the orchestrator uses for you: a model value from the Preferred Model dropdown (unknown values are ignored), or null to use the default the agent is configured with. Setting null, or a model without thinking support, also clears the thinking settings'
        ),
      enableThinking: z
        .boolean()
        .nullable()
        .describe(
          'Extended thinking for the preferred model. Only applies when preferredModel is set and supports thinking; ignored otherwise. null = not set (agent default)'
        ),
      thinkingLevel: z
        .enum(['minimal', 'low', 'medium', 'high', 'xhigh'])
        .nullable()
        .describe(
          'How much the preferred model thinks. Setting a level also turns extended thinking on. A level the model does not offer falls back to its lowest offered level'
        ),
      language: z.enum(['en', 'de', 'fr']).describe('Language the AI agent responds in: en English, de Deutsch, fr Français'),
      timezone: z
        .string()
        .min(1)
        // Refused, not silently dropped: the page's setter ignores an unknown zone, and an
        // apply that reports it written would leave the assistant believing a value the form never took.
        .refine((v) => TIMEZONES.has(v), 'not a timezone the dropdown offers')
        .describe(
          'Your IANA timezone, one the Timezone dropdown offers (e.g. "Europe/Zurich", "America/New_York", "UTC"); used to resolve relative times like "tomorrow" or "next week". Unknown zones are rejected'
        ),
      customPrompt: z
        .string()
        .describe('Your custom prompt, prepended to your conversations with AI agents. Empty string = none'),
      mcpTools: z
        .array(z.string().min(1))
        .describe(
          'Names of the MCP tools enabled for the orchestrator agent (replaces the whole list). Only settable on the MCP Tools tab'
        ),
    }),
    highlightLabels: {
      preferredModel: 'Preferred Model',
      enableThinking: 'Extended Thinking',
      thinkingLevel: 'Thinking Level',
      language: 'Language',
      timezone: 'Timezone',
      customPrompt: 'Custom Prompt',
    },
    label: () => 'Your settings',
  },
  // An agent's AGENTS.md for one scope; id is `<agent name>/<'personal' | group id>`.
  Playbook: {
    singular: 'Playbook',
    idShape: 'simple-string',
    schema: z.object({
      content: z
        .string()
        .describe(
          "The playbook (AGENTS.md) in Markdown: instructions and preferences added to the agent's behaviour. Personal playbooks apply only to you and override group playbooks on conflict; group playbooks apply to every member of the group"
        ),
    }),
    label: ({ id }) => {
      const [agent, scope] = String(id).split('/');
      return `Playbook for ${agent} (${scope === 'personal' ? 'personal' : `group ${scope}`})`;
    },
  },
  DeliveryChannel: {
    singular: 'Delivery channel',
    idShape: 'simple-numeric',
    schema: z.object({
      name: z.string().min(1).describe('Channel name, shown in pickers and the channel list'),
      description: z
        .string()
        .describe(
          'What the channel delivers and to whom; the LLM reads it to choose a channel for a notification. Empty string = none'
        ),
      webhookUrl: z.url().describe('URL scheduled-job notifications are POSTed to'),
    }),
    highlightLabels: { name: 'Name', description: 'Description', webhookUrl: 'Webhook URL' },
  },
  // A Google Drive source being added to a catalog (the add-source wizard).
  CatalogSource: {
    singular: 'Catalog source',
    idShape: 'nested',
    parentSingular: 'catalog',
    schema: z.object({
      excludeFolderPatterns: z
        .array(
          z
            .string()
            .min(1)
            .refine((p) => p === p.trim().toLowerCase(), 'Lowercase, without surrounding spaces')
        )
        .describe(
          'Folders whose name contains any of these lowercase substrings are skipped during sync (e.g. "archive", "old", "backup"). Replaces the whole list'
        ),
    }),
  },
  Secret: {
    singular: 'Secret',
    idShape: 'simple-numeric',
    schema: z.object({
      name: z.string().min(1).describe('Unique name identifying the secret, e.g. "my-foundry-secret"'),
      description: z.string().describe('What the secret is used for (optional)'),
    }),
    highlightLabels: { name: 'Name', description: 'Description' },
  },
} satisfies ObjectTypeRegistry;
