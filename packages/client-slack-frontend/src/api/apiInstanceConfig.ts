import type { CreateClientConfig } from "./generated/client.gen"

// is imported in openapi-ts.config.ts
export const createClientConfig: CreateClientConfig = (configuration) => ({
  ...configuration,
  baseUrl: `/`,
})
