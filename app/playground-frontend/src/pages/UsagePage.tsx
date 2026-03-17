import { useState, useMemo, useEffect } from 'react';
import { useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { Download, ChevronDown, ChevronRight, MessageSquare, Copy, Check, Calendar, X, Filter, ExternalLink } from 'lucide-react';
import {
  getMyUsageSummaryApiV1UsageMySummaryGetOptions,
  getMyDetailedUsageApiV1UsageMyDetailedGetOptions,
  getMyUsageLogsApiV1UsageMyLogsGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';
import type { UsageBySubAgent, BillingUnitBreakdown, UsageLog } from '@/api/generated';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from "@/components/ui/table";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, ResponsiveContainer } from "recharts";
import {
  isTokenType,
  categorizeTokenType,
  getBillingUnitLabel,
  getBillingUnitIcon,
  getBillingUnitColorClass,
  groupBillingUnits,
} from '@/lib/billing-units';
import { useAuth } from '@/contexts/AuthContext';
import { config } from '@/config';

export function UsagePage() {
  const { isAdmin } = useAuth();
  const [searchParams] = useSearchParams();
  const conversationIdParam = searchParams.get('conversation_id');
  
  const [days, setDays] = useState('30');
  const [logPage, setLogPage] = useState(1);
  const [expandedConversations, setExpandedConversations] = useState<Set<string>>(new Set());
  const [copiedConversationId, setCopiedConversationId] = useState<string | null>(null);
  const logLimit = 50;

  // Auto-expand conversation from query param
  useEffect(() => {
    if (conversationIdParam && !expandedConversations.has(conversationIdParam)) {
      setExpandedConversations(prev => new Set(prev).add(conversationIdParam));
      // Scroll to the conversation after a brief delay to let the page render
      setTimeout(() => {
        const element = document.getElementById(`conversation-${conversationIdParam}`);
        if (element) {
          element.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }, 100);
    }
  }, [conversationIdParam]);

  const { data: summary, isLoading: summaryLoading } = useQuery({
    ...getMyUsageSummaryApiV1UsageMySummaryGetOptions({
      query: { days: parseInt(days) },
    }),
  });

  const { data: detailed, isLoading: detailedLoading } = useQuery({
    ...getMyDetailedUsageApiV1UsageMyDetailedGetOptions({
      query: { days: parseInt(days) },
    }),
  });

  const { data: logsData, isLoading: logsLoading } = useQuery({
    ...getMyUsageLogsApiV1UsageMyLogsGetOptions({
      query: { 
        page: logPage, 
        limit: logLimit,
        days: parseInt(days),
        conversation_id: conversationIdParam || undefined, // Filter by conversation if provided
      },
    }),
  });

  const logs = logsData?.logs ?? [];
  const meta = logsData ?? { page: 1, limit: logLimit, total: 0 };

  // Group logs by conversation
  const conversationGroups = useMemo(() => {
    const groups = new Map<string, UsageLog[]>();
    
    logs.forEach((log: UsageLog) => {
      const conversationKey = log.conversation_id || '_no_conversation';
      if (!groups.has(conversationKey)) {
        groups.set(conversationKey, []);
      }
      groups.get(conversationKey)!.push(log);
    });
    
    // Sort each group by invoked_at (most recent first)
    groups.forEach((logs) => {
      logs.sort((a, b) => new Date(b.invoked_at).getTime() - new Date(a.invoked_at).getTime());
    });
    
    // Convert to array and sort by most recent activity
    return Array.from(groups.entries())
      .map(([conversationId, logs]) => ({
        conversationId,
        logs,
        totalCost: logs.reduce((sum, log) => sum + parseFloat(log.total_cost_usd), 0),
        totalUnits: logs.reduce((sum, log) => 
          sum + (log.billing_unit_details?.reduce((s: number, b: { unit_count: number }) => s + b.unit_count, 0) || 0), 0
        ),
        callCount: logs.length,
        mostRecent: new Date(logs[0].invoked_at),
        oldestCall: new Date(logs[logs.length - 1].invoked_at),
        scheduledJobName: logs[0]?.scheduled_job_name ?? null,
        scheduledJobId: logs[0]?.scheduled_job_id ?? null,
      }))
      .sort((a, b) => b.mostRecent.getTime() - a.mostRecent.getTime());
  }, [logs]);

  const toggleConversation = (conversationId: string) => {
    setExpandedConversations(prev => {
      const newSet = new Set(prev);
      if (newSet.has(conversationId)) {
        newSet.delete(conversationId);
      } else {
        newSet.add(conversationId);
      }
      return newSet;
    });
  };

  // Debug: Log the first record to see what data we're receiving
  if (logs.length > 0) {
    console.log('First log record:', logs[0]);
  }

  const handleExportCSV = () => {
    if (!logs.length) return;

    const headers = ['Date', 'Sub-Agent ID', 'Config Version', 'Provider', 'Model', 'Total Units', 'Cost (USD)'];
    const rows = logs.map((log: UsageLog) => [
      new Date(log.invoked_at).toISOString(),  // Use ISO format to preserve timezone
      log.sub_agent_id?.toString() || 'N/A',
      log.sub_agent_config_version_id?.toString() || 'N/A',
      log.provider || 'N/A',
      log.model_name || 'N/A',
      (log.billing_unit_details?.reduce((sum: number, b: { unit_count: number }) => sum + b.unit_count, 0) || 0).toString(),
      parseFloat(log.total_cost_usd).toFixed(6),  // Format cost as decimal
    ]);

    // Properly escape CSV values (quote fields that contain commas, quotes, or newlines)
    const escapeCsvValue = (value: string) => {
      if (value.includes(',') || value.includes('"') || value.includes('\n')) {
        return `"${value.replace(/"/g, '""')}"`;
      }
      return value;
    };

    const csv = [headers, ...rows]
      .map((row) => row.map(escapeCsvValue).join(','))
      .join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `usage-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Prepare chart data from detailed usage
  const bySubAgentChartData = useMemo(() => {
    return detailed?.by_sub_agent?.map((item: UsageBySubAgent) => ({
      name: item.sub_agent_name || 'Unknown',
      cost: parseFloat(item.total_cost_usd || '0'),
      tokens: (item.total_input_tokens || 0) + (item.total_output_tokens || 0),
    })) || [];
  }, [detailed]);

  // Calculate total tokens from billing unit breakdown
  const totalInputTokens = useMemo(() => {
    return detailed?.billing_unit_breakdown?.reduce((sum: number, item: BillingUnitBreakdown) => {
      if (isTokenType(item.billing_unit) && categorizeTokenType(item.billing_unit) === 'input') {
        return sum + (item.total_count || 0);
      }
      return sum;
    }, 0) || 0;
  }, [detailed]);

  const totalOutputTokens = useMemo(() => {
    return detailed?.billing_unit_breakdown?.reduce((sum: number, item: BillingUnitBreakdown) => {
      if (isTokenType(item.billing_unit) && categorizeTokenType(item.billing_unit) === 'output') {
        return sum + (item.total_count || 0);
      }
      return sum;
    }, 0) || 0;
  }, [detailed]);

  const totalTokens = totalInputTokens + totalOutputTokens;

  // Calculate total custom billing units (non-token resources)
  const totalCustomUnits = useMemo(() => {
    return detailed?.billing_unit_breakdown?.reduce((sum: number, item: BillingUnitBreakdown) => {
      if (!isTokenType(item.billing_unit)) {
        return sum + (item.total_count || 0);
      }
      return sum;
    }, 0) || 0;
  }, [detailed]);

  if (summaryLoading || detailedLoading || logsLoading) {
    return <div className="flex justify-center items-center h-screen">Loading...</div>;
  }

  return (
    <div className="container mx-auto p-6 space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold">Usage & Costs</h1>
          <p className="text-muted-foreground mt-1">Track your LLM usage and costs across all sub-agents</p>
        </div>
        <div className="flex items-center gap-2">
          <Label htmlFor="days-filter">Time Period:</Label>
          <Select value={days} onValueChange={setDays}>
            <SelectTrigger id="days-filter" className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="7">Last 7 days</SelectItem>
              <SelectItem value="30">Last 30 days</SelectItem>
              <SelectItem value="90">Last 90 days</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Total Cost</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">${parseFloat(summary?.total_cost_usd || '0').toFixed(4)}</div>
            <p className="text-xs text-muted-foreground mt-1">Last {days} days</p>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Total Units</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{(totalTokens + totalCustomUnits).toLocaleString()}</div>
            <p className="text-xs text-muted-foreground mt-1">
              {totalTokens.toLocaleString()} tokens ({totalInputTokens.toLocaleString()} in • {totalOutputTokens.toLocaleString()} out)
            </p>
            {totalCustomUnits > 0 && (
              <p className="text-xs text-muted-foreground">
                {totalCustomUnits.toLocaleString()} custom units
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardDescription>API Calls</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{summary?.total_requests || 0}</div>
            <p className="text-xs text-muted-foreground mt-1">Average: {summary?.total_requests && totalTokens ? (totalTokens / summary.total_requests).toFixed(0) : 0} tokens/call</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Avg Cost per Call</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              ${summary?.total_requests ? (parseFloat(summary.total_cost_usd) / summary.total_requests).toFixed(4) : '0.0000'}
            </div>
            <p className="text-xs text-muted-foreground mt-1">Per API request</p>
          </CardContent>
        </Card>
      </div>

      {/* Cost by Sub-Agent Chart */}
      {bySubAgentChartData.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Cost by Sub-Agent</CardTitle>
            <CardDescription>Total cost breakdown by sub-agent</CardDescription>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={bySubAgentChartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" />
                <YAxis tickFormatter={(value: number | undefined) => value !== undefined ? `$${value.toFixed(2)}` : '$0'} />
                <RechartsTooltip formatter={(value: number | undefined) => value !== undefined ? `$${value.toFixed(4)}` : '$0'} />
                <Bar dataKey="cost" fill="#8b5cf6" />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}

      {/* Token Type Breakdown */}
      {detailed?.billing_unit_breakdown && detailed.billing_unit_breakdown.length > 0 && (
        <>
          {/* LLM Token Usage */}
          {detailed.billing_unit_breakdown.some((item: BillingUnitBreakdown) => isTokenType(item.billing_unit)) && (
            <Card>
              <CardHeader>
                <CardTitle>🔤 LLM Token Usage</CardTitle>
                <CardDescription>
                  Detailed breakdown of token consumption. Input includes base + cache creation + audio. Output includes base + cache reads + reasoning.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  {detailed.billing_unit_breakdown
                    .filter((item: BillingUnitBreakdown) => isTokenType(item.billing_unit))
                    .map((item: BillingUnitBreakdown) => {
                      const category = categorizeTokenType(item.billing_unit);
                      return (
                        <div key={item.billing_unit} className="p-4 border rounded-lg">
                          <div className="text-sm font-medium text-muted-foreground mb-1">
                            <Badge 
                              variant="outline" 
                              className={getBillingUnitColorClass(item.billing_unit)}
                            >
                              {getBillingUnitLabel(item.billing_unit)}
                            </Badge>
                          </div>
                          <div className="text-xl font-bold">{item.total_count.toLocaleString()}</div>
                          <div className="text-sm text-muted-foreground">
                            {item.percentage.toFixed(1)}% • {category}
                          </div>
                        </div>
                      );
                    })}
                </div>
              </CardContent>
            </Card>
          )}

          {/* Custom Billing Units */}
          {detailed.billing_unit_breakdown.some((item: BillingUnitBreakdown) => !isTokenType(item.billing_unit)) && (
            <Card>
              <CardHeader>
                <CardTitle>⚙️ Custom Billing Units</CardTitle>
                <CardDescription>Non-token resource usage (API calls, searches, computations, etc.)</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  {detailed.billing_unit_breakdown
                    .filter((item: BillingUnitBreakdown) => !isTokenType(item.billing_unit))
                    .map((item: BillingUnitBreakdown) => (
                      <div key={item.billing_unit} className="p-4 border rounded-lg">
                        <div className="text-sm font-medium text-muted-foreground mb-1">
                          <Badge 
                            variant="outline"
                            className={getBillingUnitColorClass(item.billing_unit)}
                          >
                            {getBillingUnitLabel(item.billing_unit)}
                          </Badge>
                        </div>
                        <div className="text-xl font-bold">{item.total_count.toLocaleString()}</div>
                        <div className="text-sm text-muted-foreground">{item.percentage.toFixed(1)}%</div>
                      </div>
                    ))}
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}

      {/* Usage Logs Table */}
      <Card>
        <CardHeader>
          <div className="flex justify-between items-center">
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <CardTitle>Usage Logs by Conversation</CardTitle>
                {conversationIdParam && (
                  <Badge variant="secondary" className="flex items-center gap-1">
                    <Filter className="h-3 w-3" />
                    Filtered
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-4 w-4 p-0 ml-1 hover:bg-transparent"
                      onClick={() => {
                        window.location.href = '/app/usage';
                      }}
                      title="Clear filter and show all conversations"
                    >
                      <X className="h-3 w-3" />
                    </Button>
                  </Badge>
                )}
              </div>
              <CardDescription>
                {conversationIdParam
                  ? `Showing logs for conversation ${conversationIdParam.substring(0, 8)}... on this page.`
                  : 'Grouped by conversation on this page. Note: Long conversations may span multiple pages.'
                }
              </CardDescription>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={handleExportCSV}
              disabled={logs.length === 0}
            >
              <Download className="w-4 h-4 mr-2" />
              Export CSV
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="border rounded-md">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10"></TableHead>
                  <TableHead>Conversation / Call</TableHead>
                  <TableHead>Agent</TableHead>
                  <TableHead>Provider / Model</TableHead>
                  <TableHead>Unit Breakdown</TableHead>
                  <TableHead className="text-right">Units</TableHead>
                  <TableHead className="text-right">Cost (USD)</TableHead>
                  {isAdmin && <TableHead className="text-center w-16">Trace</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
              {conversationGroups.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={isAdmin ? 8 : 7} className="text-center text-muted-foreground">
                    No usage data found
                  </TableCell>
                </TableRow>
              ) : (
                conversationGroups.map(({ conversationId, logs, totalCost, totalUnits, callCount, mostRecent, oldestCall, scheduledJobName, scheduledJobId }) => {
                  const isExpanded = expandedConversations.has(conversationId);
                  const isNoConversation = conversationId === '_no_conversation';
                  const isCopied = copiedConversationId === conversationId;
                  
                  const copyConversationId = () => {
                    navigator.clipboard.writeText(conversationId);
                    setCopiedConversationId(conversationId);
                    setTimeout(() => setCopiedConversationId(null), 2000);
                  };
                  
                  return (
                    <>
                      {/* Conversation Summary Row */}
                      <TableRow
                        key={conversationId}
                        id={`conversation-${conversationId}`}
                        className="bg-muted/50 hover:bg-muted cursor-pointer font-medium"
                        onClick={() => toggleConversation(conversationId)}
                      >
                        <TableCell>
                          {isExpanded ? (
                            <ChevronDown className="w-4 h-4" />
                          ) : (
                            <ChevronRight className="w-4 h-4" />
                          )}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-2">
                            <MessageSquare className="w-4 h-4 text-muted-foreground" />
                            <div className="flex-1">
                              <div className="flex items-center gap-2">
                                {isNoConversation ? (
                                  <span className="text-muted-foreground">Direct API Calls</span>
                                ) : (
                                  <>
                                    <span className="font-mono text-sm">{conversationId}</span>
                                    <TooltipProvider>
                                      <Tooltip open={isCopied ? true : undefined}>
                                        <TooltipTrigger asChild>
                                          <Button
                                            variant="ghost"
                                            size="sm"
                                            className="h-6 w-6 p-0 hover:bg-muted-foreground/20"
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              copyConversationId();
                                            }}
                                          >
                                            {isCopied ? (
                                              <Check className="w-3 h-3 text-green-600" />
                                            ) : (
                                              <Copy className="w-3 h-3" />
                                            )}
                                          </Button>
                                        </TooltipTrigger>
                                        <TooltipContent>
                                          <p>{isCopied ? 'Copied!' : 'Copy conversation ID'}</p>
                                        </TooltipContent>
                                      </Tooltip>
                                    </TooltipProvider>

                                  </>
                                )}
                              </div>
                              {scheduledJobId && (
                                <div className="flex items-center gap-1 mt-0.5">
                                  <Calendar className="w-3 h-3 text-muted-foreground" />
                                  <span className="text-xs font-medium text-muted-foreground">
                                    Scheduled: {scheduledJobName || `Job #${scheduledJobId}`}
                                  </span>
                                </div>
                              )}
                              <div className="text-xs text-muted-foreground">
                                {callCount} call{callCount !== 1 ? 's' : ''} on this page • {mostRecent.toLocaleDateString()}
                                {callCount > 1 && ` - ${oldestCall.toLocaleDateString()}`}
                              </div>
                            </div>
                          </div>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {logs.length > 1 ? `${logs.length} calls` : logs[0].sub_agent_name || 'Orchestrator'}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {new Set(logs.map(l => l.provider).filter(Boolean)).size} provider{new Set(logs.map(l => l.provider).filter(Boolean)).size !== 1 ? 's' : ''}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {callCount} API call{callCount !== 1 ? 's' : ''}
                        </TableCell>
                        <TableCell className="text-right font-medium">{totalUnits.toLocaleString()}</TableCell>
                        <TableCell className="text-right font-bold">${totalCost.toFixed(4)}</TableCell>
                        {isAdmin && (
                          <TableCell className="text-center">
                            {!isNoConversation && (
                              <a
                                href={`https://eu.smith.langchain.com/o/${config.langsmith.organizationId}/projects/p/${config.langsmith.projectId}/t/${conversationId}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                                title="View trace in LangSmith"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <ExternalLink className="h-3 w-3" />
                              </a>
                            )}
                          </TableCell>
                        )}
                      </TableRow>

                      {/* Individual Call Rows (shown when expanded) */}
                      {isExpanded && logs.map((log: UsageLog) => {
                        const totalLogUnits = log.billing_unit_details?.reduce((sum: number, b: { unit_count: number }) => sum + b.unit_count, 0) || 0;
                        
                        // Group billing unit details by billing unit
                        const billingBreakdown = log.billing_unit_details?.reduce((acc: Record<string, number>, b: { billing_unit: string; unit_count: number }) => {
                          acc[b.billing_unit] = (acc[b.billing_unit] || 0) + b.unit_count;
                          return acc;
                        }, {} as Record<string, number>) || {};
                        
                        // Separate tokens from custom billing units
                        const { tokens, customUnits } = groupBillingUnits(billingBreakdown);
                        
                        return (
                          <TableRow key={log.id} className="bg-background">
                            <TableCell></TableCell>
                            <TableCell className="text-sm pl-8">
                              <div className="text-xs text-muted-foreground">
                                {new Date(log.invoked_at).toLocaleString()}
                              </div>
                            </TableCell>
                            <TableCell className="text-xs">
                              {log.sub_agent_id ? (
                                <div className="flex flex-col gap-1">
                                  <div className="font-medium">{log.sub_agent_name || `Agent ${log.sub_agent_id}`}</div>
                                  <div className="flex gap-1 text-[10px] text-muted-foreground">
                                    <span>ID: {log.sub_agent_id}</span>
                                    {log.sub_agent_config_version_id && (
                                      <span>• Config: {log.sub_agent_config_version_id}</span>
                                    )}
                                  </div>
                                  {log.scheduled_job_id && (
                                    <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                                      <Calendar className="w-3 h-3" />
                                      <span>{log.scheduled_job_name || `Job #${log.scheduled_job_id}`}</span>
                                    </div>
                                  )}
                                </div>
                              ) : log.scheduled_job_id ? (
                                <div className="flex items-center gap-1 text-muted-foreground">
                                  <Calendar className="w-3 h-3" />
                                  <span>{log.scheduled_job_name || `Job #${log.scheduled_job_id}`}</span>
                                </div>
                              ) : (
                                <span className="text-muted-foreground">Orchestrator</span>
                              )}
                            </TableCell>
                            <TableCell>
                              <div className="flex flex-col gap-1">
                                {log.provider && <Badge variant="outline" className="text-xs">{log.provider}</Badge>}
                                <span className="font-mono text-xs text-muted-foreground">
                                  {log.model_name || <span className="text-muted-foreground">N/A</span>}
                                </span>
                              </div>
                            </TableCell>
                            <TableCell className="text-xs">
                              <div className="flex flex-col gap-2">
                                {/* LLM Tokens */}
                                {Object.keys(tokens).length > 0 && (
                                  <div className="flex flex-col gap-1">
                                    <span className="text-[10px] font-semibold text-muted-foreground">Tokens:</span>
                                    {Object.entries(tokens).map(([type, count]) => (
                                      <div key={type} className="flex items-center gap-1">
                                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${getBillingUnitColorClass(type)}`}>
                                          {getBillingUnitLabel(type)}
                                        </span>
                                        <span className="text-muted-foreground">{count.toLocaleString()}</span>
                                      </div>
                                    ))}
                                  </div>
                                )}
                                
                                {/* Custom Billing Units */}
                                {Object.keys(customUnits).length > 0 && (
                                  <div className="flex flex-col gap-1">
                                    <span className="text-[10px] font-semibold text-muted-foreground">Custom:</span>
                                    {Object.entries(customUnits).map(([type, count]) => (
                                      <div key={type} className="flex items-center gap-1">
                                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${getBillingUnitColorClass(type)}`}>
                                          {getBillingUnitIcon(type)} {getBillingUnitLabel(type)}
                                        </span>
                                        <span className="text-muted-foreground">{count.toLocaleString()}</span>
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </div>
                            </TableCell>
                            <TableCell className="text-right">{totalLogUnits.toLocaleString()}</TableCell>
                            <TableCell className="text-right font-medium">
                              ${parseFloat(log.total_cost_usd).toFixed(4)}
                            </TableCell>
                            {isAdmin && (
                              <TableCell className="text-center">
                                {log.conversation_id && (
                                  <a
                                    href={`https://eu.smith.langchain.com/o/${config.langsmith.organizationId}/projects/p/${config.langsmith.projectId}/t/${log.conversation_id}`}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                                    title="View trace in LangSmith"
                                  >
                                    <ExternalLink className="h-3 w-3" />
                                  </a>
                                )}
                              </TableCell>
                            )}
                          </TableRow>
                        );
                      })}
                    </>
                  );
                })
              )}
              </TableBody>
            </Table>
          </div>

          {/* Pagination */}
          {meta.total > logLimit && (
            <div className="flex justify-between items-center mt-4">
              <div className="text-sm text-muted-foreground">
                Showing {(logPage - 1) * logLimit + 1} to {Math.min(logPage * logLimit, meta.total)} of {meta.total} logs
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setLogPage((p) => Math.max(1, p - 1))}
                  disabled={logPage === 1}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setLogPage((p) => p + 1)}
                  disabled={logPage * logLimit >= meta.total}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
