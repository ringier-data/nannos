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
} from './apiInstanceConfig';

// Add request interceptor to inject X-Admin-Mode header
client.interceptors.request.use((request) => {
  const adminMode = getAdminModeFromStorage();
  request.headers.set(ADMIN_MODE_HEADER, adminMode ? 'true' : 'false');
  return request;
});

export { client };
