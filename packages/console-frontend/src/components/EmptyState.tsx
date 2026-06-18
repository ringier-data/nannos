import { type ReactNode } from 'react';
import { Inbox, type LucideIcon } from 'lucide-react';
import { TableCell, TableRow } from '@/components/ui/table';
import { cn } from '@/lib/utils';

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}

/**
 * Consistent empty state: centered icon + title + optional description/action.
 * Use this instead of bare "No items found" text so empty lists look the same
 * everywhere.
 */
export function EmptyState({
  icon: Icon = Inbox,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-2 py-12 text-center',
        className,
      )}
    >
      <Icon className="h-8 w-8 text-muted-foreground" />
      <p className="text-sm font-medium">{title}</p>
      {description && (
        <p className="max-w-sm text-sm text-muted-foreground">{description}</p>
      )}
      {action}
    </div>
  );
}

/** An `<EmptyState>` rendered as a full-width row inside a shadcn `<TableBody>`. */
export function TableEmptyRow({
  colSpan,
  ...props
}: EmptyStateProps & { colSpan: number }) {
  return (
    <TableRow className="hover:bg-transparent">
      <TableCell colSpan={colSpan}>
        <EmptyState {...props} />
      </TableCell>
    </TableRow>
  );
}
