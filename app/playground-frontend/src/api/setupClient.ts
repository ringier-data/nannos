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
