import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getCurrentUserApiV1AuthMeGetOptions,
  toggleAdminModeApiV1AuthAdminModePostMutation,
} from '../api/generated/@tanstack/react-query.gen';
import {
  ADMIN_MODE_STORAGE_KEY,
  getAdminModeFromStorage,
  setAdminModeInStorage,
  IMPERSONATE_USER_STORAGE_KEY,
  getImpersonatedUserIdFromStorage,
  setImpersonatedUserIdInStorage,
  clearImpersonatedUserId,
} from '../api/apiInstanceConfig';

// Permission types
export type PermissionAction = 'read' | 'write' | 'approve';
export type PermissionResource = 'sub_agents' | 'users';

export interface UserPermissions {
  [resource: string]: PermissionAction[];
}

export interface UserGroup {
  id: number;
  name: string;
  group_role: 'read' | 'write' | 'manager';
}

interface User {
  id: string;
  email: string;
  name?: string;
  is_administrator?: boolean;
  role?: 'member' | 'approver' | 'admin';
  groups?: UserGroup[];
  [key: string]: unknown;
}

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  error: Error | null;
  permissions: UserPermissions;
  hasPermission: (resource: PermissionResource, action: PermissionAction) => boolean;
  /** Whether the user is an admin (has is_administrator=true) */
  isAdmin: boolean;
  /** Whether the user is a manager in at least one group */
  isGroupManager: boolean;
  /** Whether admin mode is currently enabled (only meaningful if isAdmin=true) */
  adminMode: boolean;
  /** Toggle admin mode on/off. Only works if user is an admin. */
  toggleAdminMode: () => void;
  /** Set admin mode explicitly. Only works if user is an admin. */
  setAdminMode: (enabled: boolean) => void;
  /** Whether currently impersonating another user */
  isImpersonating: boolean;
  /** ID of the user being impersonated, if any */
  impersonatedUserId: string | null;
  /** Start impersonating a user by ID */
  startImpersonation: (userId: string) => Promise<void>;
  /** Stop impersonating and return to admin user */
  stopImpersonation: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

// Legacy helper kept for backward compatibility
// In the new RBAC model, permissions are determined by system role + group role
function mergePermissions(): UserPermissions {
  // Return mock permissions for backward compatibility
  // Real permission checking should use the two-level RBAC logic
  return {
    sub_agents: ['read'] as PermissionAction[],
  };
}

// Machine-readable code on the backend's 503 for an impersonated user it cannot look up
// (SessionMiddleware.IMPERSONATION_UNAVAILABLE). Keyed on the code, never on the message.
const IMPERSONATION_UNAVAILABLE = 'impersonation_unavailable';

const isImpersonationUnavailable = (err: unknown): boolean =>
  !!err && typeof err === 'object' && (err as { code?: unknown }).code === IMPERSONATION_UNAVAILABLE;

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    ...getCurrentUserApiV1AuthMeGetOptions(),
    // Retry on network errors / server restarts, but not on 401 (genuinely unauthenticated).
    // The thrown error for HTTP errors is the parsed JSON body (e.g. { detail: "Not authenticated" }),
    // while network errors are TypeError instances.
    retry: (failureCount, err) => {
      // Not transient: the recovery effect below ends the impersonation instead
      if (isImpersonationUnavailable(err)) {
        return false;
      }
      // Don't retry if the backend explicitly said "not authenticated"
      if (
        err &&
        typeof err === 'object' &&
        'detail' in err &&
        (err as { detail: string }).detail === 'Not authenticated'
      ) {
        return false;
      }
      // Retry up to 3 times for network errors / transient failures (e.g., server reload)
      return failureCount < 3;
    },
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 5000),
    // Re-check auth when window regains focus (recovers from transient errors)
    refetchOnWindowFocus: true,
  });

  const user = data as User | null;
  const isAuthenticated = !!user && !error;
  // Reported as loading, not unauthenticated, so the recovery effect below refetches
  // /auth/me on the current route instead of after a round trip through /login.
  const impersonationUnavailable = isImpersonationUnavailable(error);
  const isAdmin = user?.is_administrator ?? false;
  const isGroupManager = useMemo(() => {
    return user?.groups?.some((group) => group.group_role === 'manager') ?? false;
  }, [user?.groups]);

  // Admin mode state - initialize from localStorage
  const [adminMode, setAdminModeState] = useState<boolean>(() => {
    // Only enable admin mode on mount if user is admin and it was previously enabled
    return getAdminModeFromStorage();
  });

  // Impersonation state - initialize from localStorage
  const [impersonatedUserId, setImpersonatedUserIdState] = useState<string | null>(() => {
    return getImpersonatedUserIdFromStorage();
  });

  const isImpersonating = impersonatedUserId !== null;

  // Mutation to log admin mode toggle for audit trail
  const { mutate: logAdminModeToggle } = useMutation({
    ...toggleAdminModeApiV1AuthAdminModePostMutation(),
  });

  // Sync admin mode with localStorage and invalidate queries when it changes
  const setAdminMode = useCallback(
    (enabled: boolean) => {
      // Only allow admin mode for actual admins
      if (!isAdmin && enabled) {
        return;
      }
      setAdminModeInStorage(enabled);
      setAdminModeState(enabled);
      // Log the toggle for audit trail (fire and forget - don't block UI)
      logAdminModeToggle({ body: { enabled } });
      // Invalidate all queries to refetch with new admin mode header
      queryClient.invalidateQueries();
    },
    [isAdmin, queryClient, logAdminModeToggle]
  );

  const toggleAdminMode = useCallback(() => {
    setAdminMode(!adminMode);
  }, [adminMode, setAdminMode]);

  // If user is not an admin, ensure admin mode is off
  // Only run this check after user data has loaded to avoid clearing localStorage prematurely
  useEffect(() => {
    if (!isLoading && !isAdmin && adminMode) {
      setAdminModeInStorage(false);
      setAdminModeState(false);
    }
  }, [isLoading, isAdmin, adminMode]);

  // Listen for storage changes (cross-tab sync)
  useEffect(() => {
    const handleStorageChange = (e: StorageEvent) => {
      if (e.key === ADMIN_MODE_STORAGE_KEY) {
        const newValue = e.newValue === 'true';
        setAdminModeState(newValue);
        queryClient.invalidateQueries();
      } else if (e.key === IMPERSONATE_USER_STORAGE_KEY) {
        const newValue = e.newValue;
        setImpersonatedUserIdState(newValue);
        queryClient.invalidateQueries();
      }
    };
    window.addEventListener('storage', handleStorageChange);
    return () => window.removeEventListener('storage', handleStorageChange);
  }, [queryClient]);

  // Compute merged permissions from user groups
  const permissions = useMemo(() => {
    if (!user?.groups) {
      // Mock permissions for development - remove when backend provides real data
      return {
        sub_agents: ['read', 'write', 'approve'] as PermissionAction[],
        users: ['read'] as PermissionAction[],
      };
    }
    return mergePermissions();
  }, [user?.groups]);

  const hasPermission = (resource: PermissionResource, action: PermissionAction): boolean => {
    return permissions[resource]?.includes(action) ?? false;
  };

  // Impersonation functions
  const startImpersonation = useCallback(
    async (userId: string) => {
      if (!isAdmin || !adminMode) {
        throw new Error('Must be admin with admin mode enabled to impersonate');
      }

      try {
        // Call backend to start impersonation (logs audit)
        const response = await fetch('/api/v1/admin/users/impersonate/start', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Admin-Mode': 'true', // Must include admin mode header
          },
          credentials: 'same-origin',
          body: JSON.stringify({ target_user_id: userId }),
        });

        if (!response.ok) {
          const error = await response.json().catch(() => ({ detail: 'Failed to start impersonation' }));
          throw new Error(error.detail || 'Failed to start impersonation');
        }

        // Update local state FIRST
        setImpersonatedUserIdInStorage(userId);
        setImpersonatedUserIdState(userId);

        // Force refetch all queries with new impersonation header
        // Use resetQueries to clear cache and force immediate refetch
        await queryClient.resetQueries();
      } catch (error) {
        console.error('Failed to start impersonation:', error);
        throw error;
      }
    },
    [isAdmin, adminMode, queryClient]
  );

  const stopImpersonation = useCallback(async () => {
    // Drop the impersonation locally before anything can fail or be cut short by a
    // navigation: a stored id left behind silently resumes impersonating on the next
    // request that sends it. The stop call below only records the audit event.
    clearImpersonatedUserId();
    setImpersonatedUserIdState(null);
    // Drop the target's cached data in the same moment, so nothing on screen still shows
    // the target while requests already go out as the admin; /auth/me refetches without
    // waiting on the audit call.
    const reset = queryClient.resetQueries();

    try {
      // Call backend to stop impersonation (logs audit)
      const response = await fetch('/api/v1/admin/users/impersonate/stop', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-Mode': 'true', // Must include admin mode header
        },
        credentials: 'same-origin',
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Failed to stop impersonation' }));
        throw new Error(error.detail || 'Failed to stop impersonation');
      }
    } catch (error) {
      console.error('Failed to stop impersonation:', error);
      throw error;
    } finally {
      await reset;
    }
  }, [queryClient]);

  // The backend answers 503 when it cannot look up the impersonated user. Every request
  // carries the stored id, /auth/me included, so without this the admin would be bounced
  // to /login on every attempt and could never reach the Stop button.
  useEffect(() => {
    if (impersonatedUserId && impersonationUnavailable) {
      stopImpersonation().catch(() => {});
    }
  }, [impersonatedUserId, impersonationUnavailable, stopImpersonation]);

  return (
    <AuthContext.Provider
      value={{
        user: isAuthenticated ? user : null,
        isLoading: isLoading || impersonationUnavailable,
        isAuthenticated,
        error: error as Error | null,
        permissions,
        hasPermission,
        isAdmin,
        isGroupManager,
        adminMode: isAdmin && adminMode,
        toggleAdminMode,
        setAdminMode,
        isImpersonating,
        impersonatedUserId,
        startImpersonation,
        stopImpersonation,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
