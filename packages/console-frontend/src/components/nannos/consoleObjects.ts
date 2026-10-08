import type { ObjectTypeRegistry } from '@nannos/embed-sdk';
import { adminObjectTypes } from './objects/admin';
import { catalogObjectTypes } from './objects/catalogs';
import { schedulerObjectTypes } from './objects/scheduler';
import { settingsObjectTypes } from './objects/settings';
import { skillObjectTypes } from './objects/skills';
import { subAgentObjectTypes } from './objects/subagents';

/**
 * Every console form the assistant can read and fill, one entry per object
 * type. Each entry's zod schema is the agent-settable contract: its fields and
 * `.describe()` texts are what the agent sees, and every proposed value is
 * validated against it before it reaches the form. Leave out what a form must
 * not take from the agent (secrets, credentials) and what isn't a plain field.
 *
 * Passed explicitly to `createNannosForm` (NannosForm.tsx), never registered by
 * side effect — see the SDK's object-registry.ts for why.
 */
export const consoleObjectTypes = {
  ...schedulerObjectTypes,
  ...subAgentObjectTypes,
  ...skillObjectTypes,
  ...catalogObjectTypes,
  ...settingsObjectTypes,
  ...adminObjectTypes,
} satisfies ObjectTypeRegistry;
