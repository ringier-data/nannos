import { z } from 'zod';
import type { ObjectTypeRegistry } from '@nannos/embed-sdk';

export const catalogObjectTypes = {
  Catalog: {
    singular: 'Catalog',
    idShape: 'simple-string',
    schema: z.object({
      name: z.string().min(1).describe('Catalog name, shown in the catalog list'),
      description: z.string().describe('What documents the catalog contains (optional)'),
    }),
    highlightLabels: { name: 'Name', description: 'Description' },
  },
} satisfies ObjectTypeRegistry;
