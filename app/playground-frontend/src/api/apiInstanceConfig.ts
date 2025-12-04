import type { CreateClientConfig } from './generated/client.gen';
import { config } from '../config';

// is imported in openapi-ts.config.ts
export const createClientConfig: CreateClientConfig = (configuration) => ({
  ...configuration,
  // Use apiBaseUrl from config if set, otherwise keep the default baseUrl
  ...(config.apiBaseUrl && {
    baseUrl: config.apiBaseUrl,
  }),
});
