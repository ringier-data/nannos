/**
 * Client setup with interceptors.
 *
 * This file sets up the API client interceptors. It should be imported
 * early in the application (e.g., in main.tsx) to ensure the interceptor
 * is registered before any API calls are made.
 */

import { client } from './generated/client.gen';
import {
  ADMIN_MODE_HEADER,
  getAdminModeFromStorage,
  IMPERSONATE_USER_HEADER,
  getImpersonatedUserIdFromStorage,
} from './apiInstanceConfig';

// Add request interceptor to inject X-Admin-Mode and X-Impersonate-User-Id headers
client.interceptors.request.use((request) => {
  const impersonatedUserId = getImpersonatedUserIdFromStorage();
  const adminMode = getAdminModeFromStorage();
  
  // If impersonating, admin mode MUST be true (required by backend)
  // Otherwise use the actual admin mode state
  const effectiveAdminMode = impersonatedUserId ? true : adminMode;
  
  request.headers.set(ADMIN_MODE_HEADER, effectiveAdminMode ? 'true' : 'false');
  
  if (impersonatedUserId) {
    request.headers.set(IMPERSONATE_USER_HEADER, impersonatedUserId);
    console.log('[Interceptor] Impersonation active, forcing admin mode:', impersonatedUserId);
  } else {
    console.log('[Interceptor] No impersonation, admin mode:', effectiveAdminMode);
  }
  
  return request;
});

export { client };

/**
 * Keep the HTTP status on a thrown error. The generated client throws the response
 * body (`{detail}`), so nothing could tell a 404 from a dropped request: a missing
 * sub-agent was retried with backoff before its "not found" showed. Non-enumerable,
 * so the error's visible shape is unchanged.
 */
client.interceptors.error.use((error, response) => {
  if (response && error && typeof error === 'object' && !('status' in error)) {
    Object.defineProperty(error, 'status', { value: response.status, enumerable: false });
  }
  return error;
});

/** The HTTP status a client call failed with, if it got a response. */
export function httpStatusOf(error: unknown): number | undefined {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === 'number' ? status : undefined;
}
