import type { CreateClientConfig } from './generated/client.gen';
import { config } from '../config';

// LocalStorage key for admin mode
export const ADMIN_MODE_STORAGE_KEY = 'adminMode';

// Header name for admin mode (must match backend)
export const ADMIN_MODE_HEADER = 'X-Admin-Mode';

/**
 * Get admin mode state from localStorage
 */
export function getAdminModeFromStorage(): boolean {
  try {
    return localStorage.getItem(ADMIN_MODE_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

/**
 * Set admin mode state in localStorage
 */
export function setAdminModeInStorage(enabled: boolean): void {
  try {
    localStorage.setItem(ADMIN_MODE_STORAGE_KEY, enabled ? 'true' : 'false');
  } catch {
    // Ignore localStorage errors
  }
}

// is imported in openapi-ts.config.ts
export const createClientConfig: CreateClientConfig = (configuration) => ({
  ...configuration,
  // Use apiBaseUrl from config if set, otherwise keep the default baseUrl
  ...(config.apiBaseUrl && {
    baseUrl: config.apiBaseUrl,
  }),
});
