import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getCurrentUserApiV1AuthMeGetOptions, toggleAdminModeApiV1AuthAdminModePostMutation } from '../api/generated/@tanstack/react-query.gen';
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

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    ...getCurrentUserApiV1AuthMeGetOptions(),
    retry: false,
  });

  const user = data as User | null;
  const isAuthenticated = !!user && !error;
  const isAdmin = user?.is_administrator ?? false;
  const isGroupManager = useMemo(() => {
    return user?.groups?.some(group => group.group_role === 'manager') ?? false;
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
  const setAdminMode = useCallback((enabled: boolean) => {
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
  }, [isAdmin, queryClient, logAdminModeToggle]);

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
  const startImpersonation = useCallback(async (userId: string) => {
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
  }, [isAdmin, adminMode, queryClient]);

  const stopImpersonation = useCallback(async () => {
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
      
      // Clear local state FIRST
      clearImpersonatedUserId();
      setImpersonatedUserIdState(null);
      
      // Force refetch all queries without impersonation header
      // Use resetQueries to clear cache and force immediate refetch
      await queryClient.resetQueries();
    } catch (error) {
      console.error('Failed to stop impersonation:', error);
      throw error;
    }
  }, [queryClient]);

  return (
    <AuthContext.Provider
      value={{
        user: isAuthenticated ? user : null,
        isLoading,
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
