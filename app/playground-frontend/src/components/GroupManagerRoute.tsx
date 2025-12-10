import { Navigate } from 'react-router';
import { useAuth } from '@/contexts/AuthContext';

interface GroupManagerRouteProps {
  children: React.ReactNode;
}

/**
 * Route guard that requires the user to be either:
 * - A group manager (manager role in at least one group), OR
 * - An admin with admin mode enabled
 */
export function GroupManagerRoute({ children }: GroupManagerRouteProps) {
  const { isAdmin, adminMode, isGroupManager, isLoading } = useAuth();

  if (isLoading) {
    return <div>Loading...</div>;
  }

  // Allow access if user is a group manager OR an admin with admin mode enabled
  const hasAccess = isGroupManager || (isAdmin && adminMode);

  if (!hasAccess) {
    return <Navigate to="/app" replace />;
  }

  return <>{children}</>;
}
