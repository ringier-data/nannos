export interface AppConfig {
  apiBaseUrl: string;
  orchestratorUrl: string;
  keycloakBaseUrl: string;
  keycloakRealm: string;
  langsmith: {
    organizationId: string;
    projectId: string;
  };
  autoApprove: {
    maxSystemPromptLength: number;
    maxMcpToolsCount: number;
  };
}

// Defaults used until loadConfig() completes (and as fallbacks on error).
export let config: AppConfig = {
  apiBaseUrl: '',
  orchestratorUrl: '',
  keycloakBaseUrl: '',
  keycloakRealm: '',
  langsmith: { organizationId: '', projectId: '' },
  autoApprove: { maxSystemPromptLength: 500, maxMcpToolsCount: 3 },
};

/**
 * Fetch runtime configuration from the backend and populate `config`.
 * Must be called (and awaited) before React renders.
 */
export async function loadConfig(): Promise<void> {
  const res = await fetch('/api/v1/config');
  if (!res.ok) {
    throw new Error(`Failed to load app config: ${res.status} ${res.statusText}`);
  }
  const data = await res.json();
  config = { ...config, ...data };
}
