export const config = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || window.location.origin,
  orchestratorUrl: (() => {
    const domain = import.meta.env.VITE_ORCHESTRATOR_BASE_DOMAIN || 'orchestrator.d.nannos.rcplus.io';
    const protocol = domain.includes('localhost') || domain.includes('127.0.0.1') ? 'http' : 'https';
    return `${protocol}://${domain}`;
  })(),
} as const;
