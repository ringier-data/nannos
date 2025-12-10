import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Search, Calendar } from 'lucide-react';
import { listAuditLogsApiV1AdminAuditLogsGetOptions } from '@/api/generated/@tanstack/react-query.gen';
import type { AuditAction, AuditEntityType } from '@/api/generated';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Pagination } from '@/components/admin/Pagination';

const actionColors: Record<AuditAction, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  create: 'default',
  update: 'secondary',
  delete: 'destructive',
  approve: 'default',
  reject: 'destructive',
  assign: 'secondary',
  unassign: 'outline',
  admin_mode_activated: 'default',
};

const entityTypeLabels: Record<AuditEntityType, string> = {
  user: 'User',
  group: 'Group',
  sub_agent: 'Sub-Agent',
  session: 'Session',
};

export function AuditPage() {
  const [page, setPage] = useState(1);
  const [entityType, setEntityType] = useState<AuditEntityType | 'all'>('all');
  const [action, setAction] = useState<AuditAction | 'all'>('all');
  const [userId, setUserId] = useState('');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');

  const limit = 50;

  const { data: logsData, isLoading } = useQuery({
    ...listAuditLogsApiV1AdminAuditLogsGetOptions({
      query: {
        page,
        limit,
        entity_type: entityType !== 'all' ? entityType : undefined,
        action: action !== 'all' ? action : undefined,
        user_id: userId || undefined,
        from_date: fromDate || undefined,
        to_date: toDate || undefined,
      },
    }),
  });

  const logs = logsData?.data ?? [];
  const meta = logsData?.meta ?? { page: 1, limit: 50, total: 0 };

  const formatChanges = (changes: Record<string, unknown> | undefined) => {
    if (!changes || Object.keys(changes).length === 0) return '-';
    return Object.entries(changes)
      .slice(0, 3)
      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
      .join(', ');
  };

  return (
    <div className="space-y-6 p-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Audit Logs</h1>
        <p className="text-muted-foreground">Track changes and actions across the system</p>
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <Select
          value={entityType}
          onValueChange={(value) => {
            setEntityType(value as AuditEntityType | 'all');
            setPage(1);
          }}
        >
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder="Entity Type" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Types</SelectItem>
            <SelectItem value="user">User</SelectItem>
            <SelectItem value="group">Group</SelectItem>
            <SelectItem value="sub_agent">Sub-Agent</SelectItem>
          </SelectContent>
        </Select>

        <Select
          value={action}
          onValueChange={(value) => {
            setAction(value as AuditAction | 'all');
            setPage(1);
          }}
        >
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder="Action" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Actions</SelectItem>
            <SelectItem value="create">Create</SelectItem>
            <SelectItem value="update">Update</SelectItem>
            <SelectItem value="delete">Delete</SelectItem>
            <SelectItem value="approve">Approve</SelectItem>
            <SelectItem value="reject">Reject</SelectItem>
            <SelectItem value="assign">Assign</SelectItem>
            <SelectItem value="unassign">Unassign</SelectItem>
          </SelectContent>
        </Select>

        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Actor ID..."
            value={userId}
            onChange={(e) => {
              setUserId(e.target.value);
              setPage(1);
            }}
            className="pl-9 w-[180px]"
          />
        </div>

        <div className="flex items-center gap-2">
          <Calendar className="h-4 w-4 text-muted-foreground" />
          <Input
            type="date"
            value={fromDate}
            onChange={(e) => {
              setFromDate(e.target.value);
              setPage(1);
            }}
            className="w-[150px]"
          />
          <span className="text-muted-foreground">to</span>
          <Input
            type="date"
            value={toDate}
            onChange={(e) => {
              setToDate(e.target.value);
              setPage(1);
            }}
            className="w-[150px]"
          />
        </div>
      </div>

      <div className="border rounded-lg">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Timestamp</TableHead>
              <TableHead>Actor</TableHead>
              <TableHead>Entity</TableHead>
              <TableHead>Action</TableHead>
              <TableHead>Changes</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center py-8">
                  Loading...
                </TableCell>
              </TableRow>
            ) : logs.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">
                  No audit logs found
                </TableCell>
              </TableRow>
            ) : (
              logs.map((log) => (
                <TableRow key={log.id}>
                  <TableCell className="whitespace-nowrap">
                    {log.created_at
                      ? new Date(log.created_at).toLocaleString()
                      : '-'}
                  </TableCell>
                  <TableCell className="font-mono text-sm">
                    {log.actor_sub.length > 20
                      ? `${log.actor_sub.substring(0, 20)}...`
                      : log.actor_sub}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Badge variant="outline">
                        {entityTypeLabels[log.entity_type]}
                      </Badge>
                      <span className="font-mono text-sm text-muted-foreground">
                        {log.entity_id}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={actionColors[log.action]}>
                      {log.action}
                    </Badge>
                  </TableCell>
                  <TableCell className="max-w-xs truncate text-sm text-muted-foreground">
                    {formatChanges(log.changes)}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <Pagination
        page={meta.page}
        limit={meta.limit}
        total={meta.total}
        onPageChange={setPage}
      />
    </div>
  );
}
