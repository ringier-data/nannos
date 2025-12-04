import { defineConfig } from '@hey-api/openapi-ts';

const url = process.env.OVERRIDE_URL || `https://chat.d.nannos.rcplus.io/api/v1/openapi.json`;
module.exports = defineConfig({
  input: url,
  output: './src/api/generated',
  plugins: [
    {
      enums: {
        enabled: true,
        mode: 'typescript',
        case: 'preserve',
      },
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
