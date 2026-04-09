import { defineConfig } from "@hey-api/openapi-ts"

module.exports = defineConfig({
  input:
    process.env.OVERRIDE_OPENAPI_URL ||
    `https://a2a-slack.d.nannos.ringier.ch/api/v2/openapi.json`,
  output: "./src/api/generated",
  plugins: [
    {
      enums: {
        enabled: true,
        mode: "typescript",
        case: "preserve",
      },
      name: "@hey-api/typescript",
    },
    {
      name: "@hey-api/client-fetch",
      // SDK uses client instance before it's configured (before app 'config' files are loaded)
      // details - https://heyapi.dev/openapi-ts/clients/fetch#runtime-api
      runtimeConfigPath: "../../api/apiInstanceConfig",
    },
  ],
  parser: {
    transforms: {
      enums: "root",
    },
  },
})
