import { z } from 'zod';
import type { ObjectTypeRegistry } from '@nannos/embed-sdk';

// SKILL.md keeps the description as a single frontmatter line.
const singleLine = /^[^\r\n]*$/;

export const skillObjectTypes = {
  Skill: {
    singular: 'Skill',
    idShape: 'simple-string',
    schema: z.object({
      name: z
        .string()
        .min(1)
        .describe(
          'Skill name, shown in the registry list and written to the SKILL.md frontmatter. The URL slug is derived from it (lowercased, non-alphanumerics → "-")'
        ),
      description: z
        .string()
        .regex(singleLine, 'Must be a single line')
        .describe(
          'One-line summary of what the skill does and when to use it. Agents read it to decide whether to load the skill, so lead with the trigger'
        ),
      instructions: z
        .string()
        .describe('The skill instructions: the Markdown body of SKILL.md (everything after the frontmatter)'),
      visibility: z
        .enum(['public', 'private'])
        .describe('"public": every console user can find and use the skill; "private": only you'),
    }),
    highlightLabels: { description: 'Description', instructions: 'Skill Instructions' },
  },
  SubAgentSkill: {
    singular: 'Skill',
    idShape: 'simple-string',
    schema: z.object({
      description: z
        .string()
        .describe('What this skill does and when to use it; the sub-agent reads it to decide whether to load the skill'),
      instructions: z.string().describe('The skill instructions, in Markdown'),
    }),
    highlightLabels: { description: 'Description', instructions: 'Instructions' },
    // Inline skills have no id of their own: the name identifies one within the sub-agent.
    label: ({ id }) => `Sub-agent skill ${id}`,
  },
} satisfies ObjectTypeRegistry;
