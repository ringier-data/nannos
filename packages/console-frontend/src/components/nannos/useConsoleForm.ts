import { createNannosForm } from '@nannos/embed-sdk';
import { consoleObjectTypes } from './consoleObjects';

/** The console's form-binding hook, closed over its object-type registry. */
export const useConsoleForm = createNannosForm(consoleObjectTypes);
