import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Search, MoreHorizontal, ExternalLink, Bug, Loader2, FlaskConical } from 'lucide-react';
import { config } from '@/config';
import { toast } from 'sonner';
import {
  listBugReportsApiV1BugReportsGetOptions,
  listBugReportsApiV1BugReportsGetQueryKey,
  updateBugReportStatusApiV1BugReportsReportIdStatusPatchMutation,
  consoleListSubAgentsOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { BugReportResponse, BugReportStatus } from '@/api/generated';
import { client } from '@/api/generated/client.gen';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Pagination } from '@/components/admin/Pagination';
import { BugReportStatusBadge } from '@/components/admin/BugReportStatusBadge';
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';
import { Badge } from '@/components/ui/badge';

export function BugReportsPage() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    title: string;
    description: string;
    reportId: string;
    newStatus: BugReportStatus;
  } | null>(null);

  // Debug agent picker state
  const [debugPickerOpen, setDebugPickerOpen] = useState(false);
  const [pendingDebugReportId, setPendingDebugReportId] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string>('');

  const limit = 20;

  const { data: reportsData, isLoading } = useQuery({
    ...listBugReportsApiV1BugReportsGetOptions({
      query: {
        page,
        limit,
        status_filter: statusFilter !== 'all' ? statusFilter as BugReportStatus : undefined,
      },
    }),
  });

  const statusMutation = useMutation({
    ...updateBugReportStatusApiV1BugReportsReportIdStatusPatchMutation(),
    onSuccess: () => {
      toast.success('Bug report status updated');
      queryClient.invalidateQueries({ queryKey: listBugReportsApiV1BugReportsGetQueryKey() });
    },
    onError: () => {
      toast.error('Failed to update status');
    },
  });

  const debugMutation = useMutation({
    mutationFn: async (reportId: string) => {
      const response = await client.post({
        url: '/api/v1/bug-reports/{report_id}/debug',
        path: { report_id: reportId },
      });
      if (response.error) {
        const detail = (response.error as any)?.detail ?? '';
        if (typeof detail === 'string' && detail.includes('No active debug agent')) {
          throw new Error('NO_DEBUG_AGENT');
        }
        throw new Error('Failed to trigger debug agent');
      }
      return response.data;
    },
    onSuccess: () => {
      toast.success('Debug agent triggered');
      queryClient.invalidateQueries({ queryKey: listBugReportsApiV1BugReportsGetQueryKey() });
    },
    onError: (err, reportId) => {
      if (err.message === 'NO_DEBUG_AGENT') {
        // No debug agent configured — open picker
        setPendingDebugReportId(reportId);
        setDebugPickerOpen(true);
      } else {
        toast.error('Failed to trigger debug agent');
      }
    },
  });

  // Fetch available approved agents for the debug picker dialog
  const { data: agentsData } = useQuery({
    ...consoleListSubAgentsOptions({}),
    enabled: debugPickerOpen,
  });
  const availableAgents = agentsData?.items ?? [];

  // Assign system_role then trigger debug
  const assignAndDebugMutation = useMutation({
    mutationFn: async ({ agentId, reportId }: { agentId: string; reportId: string }) => {
      // 1. Assign 'debug' system_role to the selected agent
      const roleResponse = await client.put({
        url: '/api/v1/sub-agents/{sub_agent_id}/system-role',
        path: { sub_agent_id: agentId },
        query: { role: 'debug' },
      });
      if (roleResponse.error) throw new Error('Failed to assign debug role');

      // 2. Trigger debug
      const debugResponse = await client.post({
        url: '/api/v1/bug-reports/{report_id}/debug',
        path: { report_id: reportId },
      });
      if (debugResponse.error) throw new Error('Failed to trigger debug agent');
      return debugResponse.data;
    },
    onSuccess: () => {
      toast.success('Debug agent assigned and triggered');
      setDebugPickerOpen(false);
      setPendingDebugReportId(null);
      setSelectedAgentId('');
      queryClient.invalidateQueries({ queryKey: listBugReportsApiV1BugReportsGetQueryKey() });
    },
    onError: () => {
      toast.error('Failed to assign debug agent');
    },
  });

  const reports = reportsData?.data ?? [];
  const meta = reportsData?.meta ?? { page: 1, limit: 20, total: 0 };

  const filteredReports = search
    ? reports.filter((r) => r.description?.toLowerCase().includes(search.toLowerCase()))
    : reports;

  const handleStatusChange = (report: BugReportResponse, newStatus: BugReportStatus) => {
    setConfirmDialog({
      open: true,
      title: `Update Status`,
      description: `Change status of this bug report to "${newStatus}"?`,
      reportId: report.id,
      newStatus,
    });
  };

  const executeStatusChange = () => {
    if (!confirmDialog) return;
    statusMutation.mutate({
      path: { report_id: confirmDialog.reportId },
      body: { status: confirmDialog.newStatus },
    });
    setConfirmDialog(null);
  };

  const formatDate = (dateStr?: string) => {
    if (!dateStr) return '—';
    return new Date(dateStr).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const truncateId = (id: string) => id.slice(0, 8);

  return (
    <div className="space-y-6 p-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Bug Reports</h1>
        <p className="text-muted-foreground">Review and manage reported issues</p>
      </div>

      <div className="flex items-center gap-4">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search by description..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            className="pl-9"
          />
        </div>
        <Select
          value={statusFilter}
          onValueChange={(value) => {
            setStatusFilter(value);
            setPage(1);
          }}
        >
          <SelectTrigger className="w-[180px]">
            <SelectValue placeholder="Filter by status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Statuses</SelectItem>
            <SelectItem value="open">Open</SelectItem>
            <SelectItem value="acknowledged">Acknowledged</SelectItem>
            <SelectItem value="investigating">Investigating</SelectItem>
            <SelectItem value="resolved">Resolved</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-[100px]">ID</TableHead>
              <TableHead className="w-[120px]">Source</TableHead>
              <TableHead>Description</TableHead>
              <TableHead className="w-[80px]">Links</TableHead>
              <TableHead className="w-[130px]">Status</TableHead>
              <TableHead className="w-[180px]">Created</TableHead>
              <TableHead className="w-[50px]" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8 text-muted-foreground">
                  Loading...
                </TableCell>
              </TableRow>
            ) : filteredReports.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8 text-muted-foreground">
                  No bug reports found
                </TableCell>
              </TableRow>
            ) : (
              filteredReports.map((report) => (
                <TableRow key={report.id}>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {truncateId(report.id)}
                  </TableCell>
                  <TableCell>
                    <Badge variant={report.source === 'orchestrator' ? 'default' : 'outline'}>
                      {report.source}
                    </Badge>
                  </TableCell>
                  <TableCell className="max-w-[300px] truncate">
                    {report.description || '—'}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      {report.external_link && (
                        <a
                          href={report.external_link}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                          title="View linked issue"
                        >
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      )}
                      {report.conversation_id && config.langsmith.organizationId && (
                        <a
                          href={`https://eu.smith.langchain.com/o/${config.langsmith.organizationId}/projects/p/${config.langsmith.projectId}/t/${report.conversation_id}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                          title="Original conversation trace"
                        >
                          <FlaskConical className="h-3 w-3" />
                        </a>
                      )}
                      {report.debug_conversation_id && config.langsmith.organizationId && (
                        <a
                          href={`https://eu.smith.langchain.com/o/${config.langsmith.organizationId}/projects/p/${config.langsmith.projectId}/t/${report.debug_conversation_id}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                          title="Debug agent trace"
                        >
                          <Bug className="h-3 w-3" />
                        </a>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <BugReportStatusBadge status={report.status} />
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatDate(report.created_at)}
                  </TableCell>
                  <TableCell>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon" className="h-8 w-8">
                          <MoreHorizontal className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {(report.status === 'open' || report.status === 'acknowledged') && (
                          <DropdownMenuItem
                            onClick={() => debugMutation.mutate(report.id)}
                            disabled={debugMutation.isPending}
                          >
                            <Bug className="h-4 w-4 mr-2" />
                            Run Debug Agent
                          </DropdownMenuItem>
                        )}
                        {report.status === 'open' && (
                          <DropdownMenuItem
                            onClick={() => handleStatusChange(report, 'acknowledged')}
                          >
                            Acknowledge
                          </DropdownMenuItem>
                        )}
                        {(report.status === 'open' || report.status === 'acknowledged') && (
                          <DropdownMenuItem
                            onClick={() => handleStatusChange(report, 'resolved')}
                          >
                            Resolve
                          </DropdownMenuItem>
                        )}
                        {report.status === 'resolved' && (
                          <DropdownMenuItem
                            onClick={() => handleStatusChange(report, 'open')}
                          >
                            Reopen
                          </DropdownMenuItem>
                        )}
                      </DropdownMenuContent>
                    </DropdownMenu>
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

      {confirmDialog && (
        <ConfirmDialog
          open={confirmDialog.open}
          onOpenChange={(open) => !open && setConfirmDialog(null)}
          title={confirmDialog.title}
          description={confirmDialog.description}
          confirmLabel="Update"
          onConfirm={executeStatusChange}
          isLoading={statusMutation.isPending}
        />
      )}

      {/* Debug Agent Picker Dialog */}
      <Dialog open={debugPickerOpen} onOpenChange={(open) => {
        if (!open) {
          setDebugPickerOpen(false);
          setPendingDebugReportId(null);
          setSelectedAgentId('');
        }
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Select Debug Agent</DialogTitle>
            <DialogDescription>
              No debug agent is configured. Pick an approved agent to use for debugging bug reports.
              This agent will be remembered for future debug runs.
            </DialogDescription>
          </DialogHeader>
          <Select value={selectedAgentId} onValueChange={setSelectedAgentId}>
            <SelectTrigger>
              <SelectValue placeholder="Select an agent..." />
            </SelectTrigger>
            <SelectContent>
              {Array.isArray(availableAgents) && availableAgents
                .filter((a: any) => a.default_version != null)
                .map((a: any) => (
                  <SelectItem key={a.id} value={String(a.id)}>
                    {a.name} ({a.type})
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDebugPickerOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!selectedAgentId || assignAndDebugMutation.isPending}
              onClick={() => {
                if (selectedAgentId && pendingDebugReportId) {
                  assignAndDebugMutation.mutate({
                    agentId: selectedAgentId,
                    reportId: pendingDebugReportId,
                  });
                }
              }}
            >
              {assignAndDebugMutation.isPending && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Assign & Debug
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
