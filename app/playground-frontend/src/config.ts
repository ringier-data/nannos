export const config = {
  // Use empty string for relative URLs the Vite Proxy will handle it.
  apiBaseUrl: '',
  orchestratorUrl: (() => {
    const domain = import.meta.env.VITE_ORCHESTRATOR_BASE_DOMAIN || 'orchestrator.d.nannos.rcplus.io';
    const protocol = domain.includes('localhost') || domain.includes('127.0.0.1') ? 'http' : 'https';
    return `${protocol}://${domain}`;
  })(),
  langsmith: {
    organizationId: import.meta.env.VITE_LANGSMITH_ORGANIZATION_ID || 'eacaca37-6472-40d5-80b4-9206d058caef',
    projectId: import.meta.env.VITE_LANGSMITH_PROJECT_ID || 'b3d6bc99-afe9-486a-847a-091dff103a46',
  },
} as const;
