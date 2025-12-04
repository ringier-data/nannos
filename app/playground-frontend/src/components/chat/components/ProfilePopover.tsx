import { LogOut, User as UserIcon } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Button } from '@/components/ui/button';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Separator } from '@/components/ui/separator';
import { useAuth } from '@/contexts/AuthContext';
import { getInitials } from '../utils';

export function ProfilePopover() {
  const { user, isAuthenticated } = useAuth();

  const displayName =
    isAuthenticated && user
      ? user.name ||
        `${(user as { first_name?: string }).first_name || ''} ${(user as { last_name?: string }).last_name || ''}`.trim() ||
        user.email ||
        'User'
      : 'Guest';

  const email = isAuthenticated && user ? user.email : '';
  const initials = getInitials(displayName);

  const handleLogout = () => {
    const confirmed = window.confirm('Are you sure you want to log out? This will end your session.');
    if (!confirmed) return;

    // Clear local storage
    try {
      localStorage.removeItem('a2a-session-id');
      localStorage.removeItem('a2a-connection-settings');
      localStorage.removeItem('a2a_inspector_agent_card_url');
    } catch (e) {
      console.warn('Failed to clear localStorage', e);
    }

    window.location.href = '/api/v1/auth/logout';
  };

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={`Profile menu for ${displayName}`} data-testid="button-profile">
          <UserIcon className="w-4 h-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-72" align="end">
        {/* Header */}
        <div className="flex items-center gap-3 mb-4">
          <Avatar className="h-11 w-11 border border-primary/40">
            <AvatarFallback className="bg-gradient-to-br from-primary/40 to-accent text-sm font-bold">
              {initials}
            </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-popover-foreground truncate">{displayName}</div>
            {email && <div className="text-xs text-muted-foreground truncate">{email}</div>}
          </div>
        </div>

        {/* Actions */}
        {isAuthenticated && (
          <>
            <Separator className="my-3" />
            <Button
              variant="ghost"
              className="w-full justify-start text-destructive hover:text-destructive hover:bg-destructive/10"
              onClick={handleLogout}
            >
              <LogOut className="w-4 h-4 mr-2" />
              <span>Logout</span>
            </Button>
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
