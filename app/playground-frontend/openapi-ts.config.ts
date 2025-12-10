import { defineConfig } from '@hey-api/openapi-ts';

const url = process.env.OVERRIDE_URL || `http://localhost:5001/api/v1/openapi.json`;
module.exports = defineConfig({
  input: url,
  output: './src/api/generated',
  plugins: [
    {
      enums: false,  // Use union types instead of TypeScript enums for erasableSyntaxOnly compatibility
      name: '@hey-api/typescript',
    },
    {
      name: '@hey-api/client-fetch',
      // SDK uses client instance before it's configured (before app 'config' files are loaded)
      // details - https://heyapi.dev/openapi-ts/clients/fetch#runtime-api
      runtimeConfigPath: '../apiInstanceConfig',
    },
    {
      name: '@tanstack/react-query',
    },
  ],
  parser: {
    transforms: {
      enums: 'root',
    },
  },
});
