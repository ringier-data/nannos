import type { CreateClientConfig } from './generated/client.gen';
import { config } from '../config';

// LocalStorage key for admin mode
export const ADMIN_MODE_STORAGE_KEY = 'adminMode';

// LocalStorage key for impersonated user ID
export const IMPERSONATE_USER_STORAGE_KEY = 'impersonatedUserId';

// Header name for admin mode (must match backend)
export const ADMIN_MODE_HEADER = 'X-Admin-Mode';

// Header name for user impersonation (must match backend)
export const IMPERSONATE_USER_HEADER = 'X-Impersonate-User-Id';

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

/**
 * Get impersonated user ID from localStorage
 */
export function getImpersonatedUserIdFromStorage(): string | null {
  try {
    return localStorage.getItem(IMPERSONATE_USER_STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Set impersonated user ID in localStorage
 */
export function setImpersonatedUserIdInStorage(userId: string): void {
  try {
    localStorage.setItem(IMPERSONATE_USER_STORAGE_KEY, userId);
  } catch {
    // Ignore localStorage errors
  }
}

/**
 * Clear impersonated user ID from localStorage
 */
export function clearImpersonatedUserId(): void {
  try {
    localStorage.removeItem(IMPERSONATE_USER_STORAGE_KEY);
  } catch {
    // Ignore localStorage errors
  }
}

// is imported in openapi-ts.config.ts
export const createClientConfig: CreateClientConfig = (configuration) => ({
  ...configuration,
  // Always override the hardcoded baseUrl from SDK generation
  // Empty string means relative URLs, which work with Vite proxy (local) and CloudFront (production)
  baseUrl: config.apiBaseUrl,
  // Note: credentials defaults to 'same-origin' which is correct for our setup
  // All requests are same-origin: Vite proxy in dev, CloudFront in production
});
