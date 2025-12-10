import { Navigate } from 'react-router';
import { useAuth } from '@/contexts/AuthContext';

interface AdminRouteProps {
  children: React.ReactNode;
}

/**
 * Route guard that requires both admin status AND admin mode to be enabled.
 * If the user is an admin but hasn't enabled admin mode, they'll be redirected.
 */
export function AdminRoute({ children }: AdminRouteProps) {
  const { isAdmin, adminMode, isLoading } = useAuth();

  if (isLoading) {
    return <div>Loading...</div>;
  }

  // Require both admin status AND admin mode enabled
  if (!isAdmin || !adminMode) {
    return <Navigate to="/app" replace />;
  }

  return <>{children}</>;
}
