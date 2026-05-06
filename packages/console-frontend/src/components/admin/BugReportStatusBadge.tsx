import { Badge } from '@/components/ui/badge';
import type { BugReportStatus } from '@/api/generated';

interface BugReportStatusBadgeProps {
  status: BugReportStatus;
}

const statusConfig: Record<BugReportStatus, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline' }> = {
  open: { label: 'Open', variant: 'destructive' },
  acknowledged: { label: 'Acknowledged', variant: 'default' },
  investigating: { label: 'Investigating', variant: 'outline' },
  resolved: { label: 'Resolved', variant: 'secondary' },
};

export function BugReportStatusBadge({ status }: BugReportStatusBadgeProps) {
  const config = statusConfig[status] ?? { label: status, variant: 'outline' as const };

  return (
    <Badge variant={config.variant}>
      {config.label}
    </Badge>
  );
}
