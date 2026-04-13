export const config = {
  // Use empty string for relative URLs the Vite Proxy will handle it.
  apiBaseUrl: '',
  orchestratorUrl: (() => {
    const domain =
      import.meta.env.VITE_ORCHESTRATOR_BASE_DOMAIN || window.location.hostname.replace(/^[^.]+/, 'orchestrator');
    const protocol = domain.includes('localhost') || domain.includes('127.0.0.1') ? 'http' : 'https';
    return `${protocol}://${domain}`;
  })(),
  keycloakBaseUrl: import.meta.env.VITE_KEYCLOAK_BASE_URL || 'https://login.p.nannos.rcplus.io',
  keycloakRealm: import.meta.env.VITE_KEYCLOAK_REALM || 'nannos',
  langsmith: {
    organizationId: import.meta.env.VITE_LANGSMITH_ORGANIZATION_ID || 'eacaca37-6472-40d5-80b4-9206d058caef',
    projectId: import.meta.env.VITE_LANGSMITH_PROJECT_ID || 'b3d6bc99-afe9-486a-847a-091dff103a46',
  },
  autoApprove: {
    maxSystemPromptLength: Number(import.meta.env.VITE_AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH || '500'),
    maxMcpToolsCount: Number(import.meta.env.VITE_AUTO_APPROVE_MAX_MCP_TOOLS_COUNT || '3'),
  },
} as const;
