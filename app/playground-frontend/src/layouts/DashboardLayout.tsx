import { Link, Outlet } from 'react-router';
import { LogOut, Shield, ShieldOff } from 'lucide-react';
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
} from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { useAuth } from '@/contexts/AuthContext';
import { mainNavItems, groupManagerNavItems, adminNavItems } from '@/config/navigation';

export function DashboardLayout() {
  const { user, isAdmin, isGroupManager, adminMode, toggleAdminMode } = useAuth();

  const handleLogout = () => {
    window.location.href = `/api/v1/auth/logout?redirectTo=${encodeURIComponent(window.location.origin + '/')}`;
  };

  return (
    <SidebarProvider>
      <Sidebar>
        <SidebarHeader>
          <div className="flex items-center gap-2 px-2 py-2">
            <span className="font-semibold">Playground</span>
          </div>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Navigation</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {mainNavItems.map((item) => (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton asChild>
                      <Link to={item.url}>
                        <item.icon />
                        <span>{item.title}</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
          {isGroupManager && !isAdmin && (
            <SidebarGroup>
              <SidebarGroupLabel>Group Management</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {groupManagerNavItems.map((item) => (
                    <SidebarMenuItem key={item.title}>
                      <SidebarMenuButton asChild>
                        <Link to={item.url}>
                          <item.icon />
                          <span>{item.title}</span>
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          )}
          {isAdmin && (
            <SidebarGroup>
              <SidebarGroupLabel className="flex items-center justify-between">
                <span className="flex items-center gap-2">
                  {adminMode ? <Shield className="h-4 w-4" /> : <ShieldOff className="h-4 w-4" />}
                  Admin Mode
                </span>
                <Switch
                  id="admin-mode"
                  checked={adminMode}
                  onCheckedChange={toggleAdminMode}
                  aria-label="Toggle admin mode"
                />
              </SidebarGroupLabel>
              {adminMode && (
                <SidebarGroupContent>
                  <SidebarMenu>
                    {adminNavItems.map((item) => (
                      <SidebarMenuItem key={item.title}>
                        <SidebarMenuButton asChild>
                          <Link to={item.url}>
                            <item.icon />
                            <span>{item.title}</span>
                          </Link>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    ))}
                  </SidebarMenu>
                </SidebarGroupContent>
              )}
            </SidebarGroup>
          )}
        </SidebarContent>
      </Sidebar>
      <SidebarInset className="h-screen overflow-hidden">
        <header className="flex h-14 shrink-0 items-center gap-4 border-b px-4">
          <SidebarTrigger />
          <div className="flex-1" />
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">{user?.email}</span>
            <Button variant="ghost" size="icon" onClick={handleLogout} title="Logout">
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        </header>
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
