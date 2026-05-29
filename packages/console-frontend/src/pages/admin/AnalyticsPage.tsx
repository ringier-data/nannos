import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Download, TrendingUp, TrendingDown, Users, UserMinus, Activity, DollarSign } from 'lucide-react';
import {
  getActiveUsersApiV1AnalyticsActiveUsersGetOptions,
  getChurnRateApiV1AnalyticsChurnGetOptions,
  getEngagementApiV1AnalyticsEngagementGetOptions,
  getCohortsApiV1AnalyticsCohortsGetOptions,
  getCostOverTimeApiV1AnalyticsCostOverTimeGetOptions,
} from '@/api/generated/@tanstack/react-query.gen';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from '@/components/ui/chart';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  XAxis,
  YAxis,
  Line,
  LineChart,
} from 'recharts';

// ============================================================================
// Chart Configs
// ============================================================================

const activeUsersChartConfig: ChartConfig = {
  value: { label: 'Active Users', color: 'var(--chart-1)' },
};

const churnChartConfig: ChartConfig = {
  value: { label: 'Churn Rate %', color: 'var(--chart-4)' },
};

const engagementChartConfig: ChartConfig = {
  user_count: { label: 'Users', color: 'var(--chart-2)' },
};

const cohortChartConfig: ChartConfig = {
  'New (1 week)': { label: 'New (1 week)', color: 'var(--chart-1)' },
  'Young (2-4 weeks)': { label: 'Young (2-4 weeks)', color: 'var(--chart-2)' },
  'Established (1-3 months)': { label: 'Established (1-3 months)', color: 'var(--chart-3)' },
  'Veteran (3+ months)': { label: 'Veteran (3+ months)', color: 'var(--chart-4)' },
};

const costChartConfig: ChartConfig = {
  total_cost_usd: { label: 'Cost (USD)', color: 'var(--chart-5)' },
  request_count: { label: 'Requests', color: 'var(--chart-2)' },
};

// ============================================================================
// Helpers
// ============================================================================

function formatPeriod(period: string, granularity: string): string {
  const date = new Date(period);
  if (granularity === 'day') return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  if (granularity === 'week') return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  return date.toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
}

function formatChange(change: number | null | undefined): { text: string; positive: boolean } {
  if (change == null) return { text: 'N/A', positive: true };
  return { text: `${change > 0 ? '+' : ''}${change.toFixed(1)}%`, positive: change >= 0 };
}

function exportToCsv(data: Record<string, unknown>[], filename: string) {
  if (!data.length) return;
  const headers = Object.keys(data[0]);
  const csvRows = [
    headers.join(','),
    ...data.map(row => headers.map(h => JSON.stringify(row[h] ?? '')).join(',')),
  ];
  const blob = new Blob([csvRows.join('\n')], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ============================================================================
// Page Component
// ============================================================================

export function AnalyticsPage() {
  const [days, setDays] = useState('30');
  const [activeUsersGranularity, setActiveUsersGranularity] = useState<'day' | 'week'>('day');
  const [costGranularity, setCostGranularity] = useState<'day' | 'week' | 'month'>('day');

  const daysNum = parseInt(days);

  // Data fetching
  const { data: activeUsers, isLoading: activeUsersLoading } = useQuery({
    ...getActiveUsersApiV1AnalyticsActiveUsersGetOptions({
      query: { days: daysNum, granularity: activeUsersGranularity },
    }),
  });

  const { data: churn, isLoading: churnLoading } = useQuery({
    ...getChurnRateApiV1AnalyticsChurnGetOptions({
      query: { days: daysNum },
    }),
  });

  const { data: engagement, isLoading: engagementLoading } = useQuery({
    ...getEngagementApiV1AnalyticsEngagementGetOptions({
      query: { days: daysNum },
    }),
  });

  const { data: cohorts, isLoading: cohortsLoading } = useQuery({
    ...getCohortsApiV1AnalyticsCohortsGetOptions({
      query: { days: daysNum },
    }),
  });

  const { data: costOverTime, isLoading: costLoading } = useQuery({
    ...getCostOverTimeApiV1AnalyticsCostOverTimeGetOptions({
      query: { days: daysNum, granularity: costGranularity },
    }),
  });

  // Export all data as CSV
  const handleExportAll = () => {
    const rows: Record<string, unknown>[] = [];

    if (activeUsers?.data) {
      for (const d of activeUsers.data) {
        rows.push({ report_type: 'active-users', period: d.period, value: d.value });
      }
    }
    if (churn?.data) {
      for (const d of churn.data) {
        rows.push({ report_type: 'churn-rate', period: d.period, value: d.value });
      }
    }
    if (engagement?.data) {
      for (const d of engagement.data) {
        rows.push({ report_type: 'engagement', bucket: d.bucket, user_count: d.user_count, percent_of_users: d.percent_of_users });
      }
    }
    if (cohorts?.data) {
      for (const d of cohorts.data) {
        rows.push({ report_type: 'cohorts', cohort: d.cohort, user_count: d.user_count, percent_of_users: d.percent_of_users });
      }
    }
    if (costOverTime?.data) {
      for (const d of costOverTime.data) {
        rows.push({ report_type: 'cost-over-time', period: d.period, total_cost_usd: d.total_cost_usd, request_count: d.request_count });
      }
    }

    exportToCsv(rows, `analytics_${days}d_${new Date().toISOString().slice(0, 10)}.csv`);
  };

  return (
    <div className="space-y-6 p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Analytics</h1>
          <p className="text-muted-foreground text-sm">Platform health and user adoption metrics</p>
        </div>
        <div className="flex items-center gap-3">
          <Select value={days} onValueChange={setDays}>
            <SelectTrigger className="w-[130px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="7">Last 7 days</SelectItem>
              <SelectItem value="30">Last 30 days</SelectItem>
              <SelectItem value="90">Last 90 days</SelectItem>
              <SelectItem value="180">Last 6 months</SelectItem>
              <SelectItem value="365">Last year</SelectItem>
            </SelectContent>
          </Select>
          <Button variant="outline" size="sm" onClick={handleExportAll}>
            <Download className="mr-2 h-4 w-4" />
            Export All
          </Button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryCard
          title="Active Users"
          value={activeUsers?.summary?.current}
          change={activeUsers?.summary?.change_percent}
          icon={Users}
          loading={activeUsersLoading}
          subtitle={`${activeUsersGranularity === 'day' ? 'DAU' : 'WAU'} (${days}d)`}
        />
        <SummaryCard
          title="Churn Rate"
          value={churn?.summary?.churn_rate_percent != null ? `${churn.summary.churn_rate_percent}%` : undefined}
          change={null}
          icon={UserMinus}
          loading={churnLoading}
          subtitle={`${churn?.summary?.churned_users ?? 0} users churned`}
          invertColor
        />
        <SummaryCard
          title="Engaged Users"
          value={engagement?.total_active_users}
          change={null}
          icon={Activity}
          loading={engagementLoading}
          subtitle={`${days}d active with conversations`}
        />
        <SummaryCard
          title="Total Cost"
          value={costOverTime?.summary?.total_cost_usd != null ? `$${Number(costOverTime.summary.total_cost_usd).toFixed(2)}` : undefined}
          change={costOverTime?.summary?.change_percent}
          icon={DollarSign}
          loading={costLoading}
          subtitle={`${costOverTime?.summary?.total_requests ?? 0} requests`}
          invertColor
        />
      </div>

      {/* Active Users Chart */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <div>
            <CardTitle className="text-base">Active Users over Time</CardTitle>
            <CardDescription>Unique users with at least one interaction</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Select value={activeUsersGranularity} onValueChange={(v) => setActiveUsersGranularity(v as 'day' | 'week')}>
              <SelectTrigger className="w-[100px] h-8">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="day">Daily</SelectItem>
                <SelectItem value="week">Weekly</SelectItem>
              </SelectContent>
            </Select>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() => activeUsers?.data && exportToCsv(
                activeUsers.data.map(d => ({ period: d.period, active_users: d.value })),
                `active_users_${activeUsersGranularity}_${days}d.csv`
              )}
            >
              <Download className="h-4 w-4" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {activeUsersLoading ? (
            <div className="flex h-[250px] items-center justify-center text-muted-foreground">Loading...</div>
          ) : !activeUsers?.data?.length ? (
            <EmptyChart message="No active user data for this period" />
          ) : (
            <ChartContainer config={activeUsersChartConfig} className="h-[250px] w-full">
              <AreaChart accessibilityLayer data={activeUsers.data.map(d => ({ ...d, value: Number(d.value) }))}>
                <CartesianGrid vertical={false} />
                <XAxis
                  dataKey="period"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={8}
                  tickFormatter={(v) => formatPeriod(v, activeUsersGranularity)}
                />
                <YAxis tickLine={false} axisLine={false} />
                <ChartTooltip content={<ChartTooltipContent />} />
                <Area
                  type="monotone"
                  dataKey="value"
                  fill="var(--color-value)"
                  fillOpacity={0.3}
                  stroke="var(--color-value)"
                  strokeWidth={2}
                />
              </AreaChart>
            </ChartContainer>
          )}
        </CardContent>
      </Card>

      {/* Churn + Cost side by side */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Churn Rate Chart */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <div>
              <CardTitle className="text-base">Churn Rate</CardTitle>
              <CardDescription>Week-over-week user churn</CardDescription>
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() => churn?.data && exportToCsv(
                churn.data.map(d => ({ period: d.period, churn_rate_percent: d.value })),
                `churn_rate_${days}d.csv`
              )}
            >
              <Download className="h-4 w-4" />
            </Button>
          </CardHeader>
          <CardContent>
            {churnLoading ? (
              <div className="flex h-[220px] items-center justify-center text-muted-foreground">Loading...</div>
            ) : !churn?.data?.length ? (
              <EmptyChart message="Not enough data to calculate churn (requires 2+ weeks)" />
            ) : (
              <ChartContainer config={churnChartConfig} className="h-[220px] w-full">
                <LineChart accessibilityLayer data={churn.data.map(d => ({ ...d, value: Number(d.value) }))}>
                  <CartesianGrid vertical={false} />
                  <XAxis
                    dataKey="period"
                    tickLine={false}
                    axisLine={false}
                    tickMargin={8}
                    tickFormatter={(v) => formatPeriod(v, 'week')}
                  />
                  <YAxis tickLine={false} axisLine={false} tickFormatter={(v) => `${v}%`} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Line
                    type="monotone"
                    dataKey="value"
                    stroke="var(--color-value)"
                    strokeWidth={2}
                    dot={{ r: 4 }}
                  />
                </LineChart>
              </ChartContainer>
            )}
          </CardContent>
        </Card>

        {/* Cost over Time */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <div>
              <CardTitle className="text-base">Platform Cost</CardTitle>
              <CardDescription>Global LLM spend over time</CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <Select value={costGranularity} onValueChange={(v) => setCostGranularity(v as 'day' | 'week' | 'month')}>
                <SelectTrigger className="w-[100px] h-8">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="day">Daily</SelectItem>
                  <SelectItem value="week">Weekly</SelectItem>
                  <SelectItem value="month">Monthly</SelectItem>
                </SelectContent>
              </Select>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={() => costOverTime?.data && exportToCsv(
                  costOverTime.data.map(d => ({ period: d.period, cost_usd: d.total_cost_usd, requests: d.request_count })),
                  `cost_${costGranularity}_${days}d.csv`
                )}
              >
                <Download className="h-4 w-4" />
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {costLoading ? (
              <div className="flex h-[220px] items-center justify-center text-muted-foreground">Loading...</div>
            ) : !costOverTime?.data?.length ? (
              <EmptyChart message="No cost data recorded for this period" />
            ) : (
              <ChartContainer config={costChartConfig} className="h-[220px] w-full">
                <AreaChart accessibilityLayer data={costOverTime.data.map(d => ({ ...d, total_cost_usd: Number(d.total_cost_usd) }))}>
                  <CartesianGrid vertical={false} />
                  <XAxis
                    dataKey="period"
                    tickLine={false}
                    axisLine={false}
                    tickMargin={8}
                    tickFormatter={(v) => formatPeriod(v, costGranularity)}
                  />
                  <YAxis tickLine={false} axisLine={false} tickFormatter={(v) => `$${v}`} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Area
                    type="monotone"
                    dataKey="total_cost_usd"
                    fill="var(--color-total_cost_usd)"
                    fillOpacity={0.3}
                    stroke="var(--color-total_cost_usd)"
                    strokeWidth={2}
                  />
                </AreaChart>
              </ChartContainer>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Engagement + Cohorts side by side */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Engagement Distribution */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <div>
              <CardTitle className="text-base">Engagement Distribution</CardTitle>
              <CardDescription>Users by conversation frequency ({days}d)</CardDescription>
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() => engagement?.data && exportToCsv(
                engagement.data.map(d => ({ bucket: d.bucket, user_count: d.user_count, percent: d.percent_of_users })),
                `engagement_${days}d.csv`
              )}
            >
              <Download className="h-4 w-4" />
            </Button>
          </CardHeader>
          <CardContent>
            {engagementLoading ? (
              <div className="flex h-[220px] items-center justify-center text-muted-foreground">Loading...</div>
            ) : !engagement?.data?.length ? (
              <EmptyChart message="No engagement data for this period" />
            ) : (
              <ChartContainer config={engagementChartConfig} className="h-[220px] w-full">
                <BarChart accessibilityLayer data={engagement.data} layout="vertical">
                  <CartesianGrid horizontal={false} />
                  <XAxis type="number" tickLine={false} axisLine={false} />
                  <YAxis
                    type="category"
                    dataKey="bucket"
                    tickLine={false}
                    axisLine={false}
                    width={60}
                    tickFormatter={(v) => `${v} conv.`}
                  />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Bar dataKey="user_count" fill="var(--color-user_count)" radius={4} />
                </BarChart>
              </ChartContainer>
            )}
          </CardContent>
        </Card>

        {/* Cohorts */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <div>
              <CardTitle className="text-base">User Cohorts</CardTitle>
              <CardDescription>Tenure distribution ({days}d window)</CardDescription>
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() => cohorts?.data && exportToCsv(
                cohorts.data.map(d => ({ cohort: d.cohort, user_count: d.user_count, percent: d.percent_of_users })),
                `cohorts_${days}d.csv`
              )}
            >
              <Download className="h-4 w-4" />
            </Button>
          </CardHeader>
          <CardContent>
            {cohortsLoading ? (
              <div className="flex h-[220px] items-center justify-center text-muted-foreground">Loading...</div>
            ) : !cohorts?.data?.length ? (
              <EmptyChart message="No cohort data for this period" />
            ) : (
              <ChartContainer config={cohortChartConfig} className="h-[220px] w-full">
                <BarChart accessibilityLayer data={cohorts.data}>
                  <CartesianGrid vertical={false} />
                  <XAxis
                    dataKey="cohort"
                    tickLine={false}
                    axisLine={false}
                    tickMargin={8}
                    tickFormatter={(v) => v.split(' (')[0]}
                  />
                  <YAxis tickLine={false} axisLine={false} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Bar dataKey="user_count" radius={4}>
                    {cohorts.data.map((entry, index) => {
                      const colors = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)'];
                      return <rect key={entry.cohort} fill={colors[index % colors.length]} />;
                    })}
                  </Bar>
                </BarChart>
              </ChartContainer>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

// ============================================================================
// Summary Card Component
// ============================================================================

function SummaryCard({
  title,
  value,
  change,
  icon: Icon,
  loading,
  subtitle,
  invertColor = false,
}: {
  title: string;
  value: number | string | null | undefined;
  change: number | null | undefined;
  icon: React.ComponentType<{ className?: string }>;
  loading: boolean;
  subtitle?: string;
  invertColor?: boolean;
}) {
  const changeInfo = formatChange(change);
  const isPositive = invertColor ? !changeInfo.positive : changeInfo.positive;

  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between">
          <p className="text-sm font-medium text-muted-foreground">{title}</p>
          <Icon className="h-4 w-4 text-muted-foreground" />
        </div>
        <div className="mt-2">
          {loading ? (
            <div className="h-8 w-24 animate-pulse rounded bg-muted" />
          ) : (
            <p className="text-2xl font-bold">{value ?? '—'}</p>
          )}
        </div>
        <div className="mt-1 flex items-center gap-1">
          {change != null && (
            <>
              {isPositive ? (
                <TrendingUp className="h-3 w-3 text-green-600" />
              ) : (
                <TrendingDown className="h-3 w-3 text-red-600" />
              )}
              <span className={`text-xs font-medium ${isPositive ? 'text-green-600' : 'text-red-600'}`}>
                {changeInfo.text}
              </span>
            </>
          )}
          {subtitle && <span className="text-xs text-muted-foreground">{change != null ? ' · ' : ''}{subtitle}</span>}
        </div>
      </CardContent>
    </Card>
  );
}

// ============================================================================
// Empty Chart Placeholder
// ============================================================================

function EmptyChart({ message }: { message: string }) {
  return (
    <div className="flex h-[220px] flex-col items-center justify-center gap-2 text-muted-foreground">
      <Activity className="h-8 w-8 opacity-40" />
      <p className="text-sm">{message}</p>
    </div>
  );
}
