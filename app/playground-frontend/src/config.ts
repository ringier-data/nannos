export const config = {
  // Use empty string for relative URLs when VITE_API_BASE_URL is not set
  // This ensures same-origin requests work correctly with CloudFront routing
  // For local dev with remote backend, VITE_API_BASE_URL is explicitly set by start-dev.sh
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || '',
  orchestratorUrl: (() => {
    const domain = import.meta.env.VITE_ORCHESTRATOR_BASE_DOMAIN || 'orchestrator.d.nannos.rcplus.io';
    const protocol = domain.includes('localhost') || domain.includes('127.0.0.1') ? 'http' : 'https';
    return `${protocol}://${domain}`;
  })(),
} as const;
