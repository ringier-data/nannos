import { Badge } from '@/components/ui/badge';
import type { UserStatus } from '@/api/generated';

interface UserStatusBadgeProps {
  status: UserStatus;
}

const statusConfig: Record<UserStatus, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline' }> = {
  active: { label: 'Active', variant: 'default' },
  suspended: { label: 'Suspended', variant: 'secondary' },
  deleted: { label: 'Deleted', variant: 'destructive' },
};

export function UserStatusBadge({ status }: UserStatusBadgeProps) {
  const config = statusConfig[status] ?? { label: status, variant: 'outline' as const };
  
  return (
    <Badge variant={config.variant}>
      {config.label}
    </Badge>
  );
}
