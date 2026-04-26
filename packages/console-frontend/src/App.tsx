import { Routes, Route, Navigate } from 'react-router';
import { useAuth } from './contexts/AuthContext';
import { LoginRequiredPage } from './pages/LoginRequiredPage';
import { SettingsPage } from './pages/SettingsPage';
import { DashboardLayout } from './layouts/DashboardLayout';
import { ChatPage } from './pages/ChatPage';
import { SubAgentsPage } from './pages/SubAgentsPage';
import { SubAgentCreatePage } from './pages/SubAgentCreatePage';
import { SubAgentDetailPage } from './pages/SubAgentDetailPage';
import { UsagePage } from './pages/UsagePage';
import { SchedulerPage } from './pages/SchedulerPage';
import { SchedulerJobDetailPage } from './pages/SchedulerJobDetailPage';
import { DeliveryChannelsPage } from './pages/DeliveryChannelsPage';
import { CatalogsPage } from './pages/CatalogsPage';
import { CatalogDetailPage } from './pages/CatalogDetailPage';
import { AdminRoute } from './components/AdminRoute';
import { GroupManagerRoute } from './components/GroupManagerRoute';
import { UsersPage } from './pages/admin/UsersPage';
import { UserDetailPage } from './pages/admin/UserDetailPage';
import { GroupsPage } from './pages/admin/GroupsPage';
import { GroupDetailPage } from './pages/admin/GroupDetailPage';
import { AuditPage } from './pages/admin/AuditPage';
import { RateCardsPage } from './pages/admin/RateCardsPage';

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return <div>Loading...</div>;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

function App() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return <div>Loading...</div>;
  }

  return (
    <Routes>
      <Route path="/login" element={isAuthenticated ? <Navigate to="/app" replace /> : <LoginRequiredPage />} />
      <Route
        path="/app"
        element={
          <ProtectedRoute>
            <DashboardLayout />
          </ProtectedRoute>
        }
      >
        <Route index element={<SettingsPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="subagents" element={<SubAgentsPage />} />
        <Route path="subagents/new" element={<SubAgentCreatePage />} />
        <Route path="subagents/:id" element={<SubAgentDetailPage />} />
        <Route path="usage" element={<UsagePage />} />
        <Route path="scheduler" element={<SchedulerPage />} />
        <Route path="scheduler/:id" element={<SchedulerJobDetailPage />} />
        <Route path="catalogs" element={<CatalogsPage />} />
        <Route path="catalogs/:id" element={<CatalogDetailPage />} />
        <Route path="delivery-channels" element={<DeliveryChannelsPage />} />
        <Route path="groups" element={<GroupManagerRoute><GroupsPage /></GroupManagerRoute>} />
        <Route path="groups/:id" element={<GroupManagerRoute><GroupDetailPage /></GroupManagerRoute>} />
        <Route path="admin/users" element={<AdminRoute><UsersPage /></AdminRoute>} />
        <Route path="admin/users/:id" element={<AdminRoute><UserDetailPage /></AdminRoute>} />
        <Route path="admin/groups" element={<AdminRoute><GroupsPage /></AdminRoute>} />
        <Route path="admin/groups/:id" element={<AdminRoute><GroupDetailPage /></AdminRoute>} />
        <Route path="admin/audit" element={<AdminRoute><AuditPage /></AdminRoute>} />
        <Route path="admin/rate-cards" element={<AdminRoute><RateCardsPage /></AdminRoute>} />
      </Route>
      <Route path="*" element={<Navigate to="/app" replace />} />
    </Routes>
  );
}

export default App;
