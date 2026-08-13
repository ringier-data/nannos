/**
 * Fixture conversation for the dev harness.
 *
 * These are real LLM-shaped answers, not lorem ipsum: the point of the harness is
 * to prove the renderer copes with what agents actually emit — a 2-column metrics
 * block, a 3-column audit table far wider than the panel, emoji status lines, long
 * cell values, and a code block.
 */
import type { Message, TimelineEvent, TodoItem, PendingInterrupt } from '../src/ui/chat/types';

const at = (minutesAgo: number) => new Date(Date.UTC(2026, 7, 13, 10, 0, 0) - minutesAgo * 60_000);

/** The designed shape: heading, emoji findings, then a 2-column key/value block. */
const HEALTH_CHECK_ANSWER = `### KW02-03 Gstaad Tourismus Snowboarding 2026

🔴 **Pacing 12% behind** — 6 of 14 line items underdelivering; peak window closes Sat 18:00 CET

🚩 **Budget nearly exhausted** — CHF 187,320 of CHF 195,000 spent with 4 days remaining

Snowboard_15s creative has 94% delivery rate; Slope_Hero_30s at 61%

Approval needed to redistribute CHF 7,680 to top-performing placements

|  |  |
| --- | --- |
| Campaign | KW02-03 Gstaad |
| Total budget | CHF 195,000 |
| Spent to date | CHF 187,320 (96%) |
| Line items | 14 total / 6 at-risk |
| Impressions | 4.2 M / 4.8 M target |
| Days remaining | 4 |
`;

/** The table that rendered badly before: 3 columns, long values, emoji, in ~370px. */
const WIDE_AUDIT_ANSWER = `Configuration audit for **line item 4471**:

| Field | Value | Verdict |
| --- | --- | --- |
| Inventory type | guaranteed | ✅ |
| Billing method | CPM | ✅ Correct for guaranteed booking |
| Start / End | 2026-05-06 → 2026-05-29 | ⚠️ Campaign ended ~2.5 months ago (today is 2026-08-12) |
| Budget | CHF 1,000 (customerNet) | ✅ |
| Frequency cap | 3/day | ✅ |
| Customer | Test Marleen SF Aktualisierung (ID 34) | ✅ |
| maxUnitPrice | not set | ✅ Correct — guaranteed campaigns derive price from the booking |
| Creatives | 2 attached, 1 rejected | ⚠️ Slope_Hero_30s rejected by the ad server |

\`\`\`json
{ "lineItemId": 4471, "syncState": "OUT_OF_SYNC", "lastSyncedAt": "2026-05-29T21:59:00Z" }
\`\`\`

Nothing needs changing unless you plan to reactivate the flight.
`;

export const FIXTURE_TIMELINE: TimelineEvent[] = [
  { type: 'status', timestamp: at(9), message: 'Loading skill campaign-health-check…' },
  {
    type: 'status',
    timestamp: at(8),
    message: 'get_campaign, get_campaign_sync_status, list_line_items',
    source: 'Campaign Agent',
  },
  { type: 'status', timestamp: at(7), message: 'get_current_time', source: 'Campaign Agent' },
  {
    type: 'thought_end',
    timestamp: at(6),
    agent_name: 'Campaign Agent',
    content: 'Pacing is behind on 6 line items.\nChecking whether the peak window can still absorb the shortfall.',
    complete: true,
  },
];

export const FIXTURE_MESSAGES: Message[] = [
  {
    id: 'm1',
    conversationId: 'c1',
    type: 'context',
    content: 'Health check',
    injectedPrompt: 'Run a health check on campaign 452 and summarise anything that needs attention.',
    timestamp: at(10),
  },
  {
    id: 'm2',
    conversationId: 'c1',
    type: 'agent',
    content: HEALTH_CHECK_ANSWER,
    timestamp: at(6),
    timeline: FIXTURE_TIMELINE,
  },
  {
    id: 'm3',
    conversationId: 'c1',
    type: 'user',
    content: 'Now audit the config of line item 4471.',
    timestamp: at(4),
  },
  {
    id: 'm4',
    conversationId: 'c1',
    type: 'agent',
    content: WIDE_AUDIT_ANSWER,
    timestamp: at(3),
  },
];

export const FIXTURE_ERROR_MESSAGE: Message = {
  id: 'm5',
  conversationId: 'c1',
  type: 'agent',
  content: 'Error: the orchestrator closed the connection before the turn completed (request id 9f2c1a).',
  timestamp: at(1),
};

export const FIXTURE_WORKING_STEPS: TodoItem[] = [
  { name: 'Load campaign 452', state: 'completed' },
  { name: 'Check sync status', state: 'completed' },
  { name: 'Checking line item pacing for Slope_Hero_30s placements', state: 'working' },
  { name: 'Compare against booked impressions', state: 'submitted', source: 'Campaign Agent' },
  { name: 'Draft redistribution proposal', state: 'submitted', source: 'Campaign Agent' },
];

export const FIXTURE_INTERRUPT: PendingInterrupt = {
  conversationId: 'c1',
  toolName: 'update_line_items',
  reason: 'This will move CHF 7,680 of budget across 4 line items.',
  actionRequests: [
    {
      name: 'update_line_items',
      description: 'Redistribute remaining budget to the three best-performing placements.',
      args: {
        campaign_id: 452,
        line_item_ids: '4471, 4472, 4480, 4481',
        amount: 'CHF 7,680',
        strategy: 'proportional_to_delivery',
        _risk_metadata: {
          source: 'risk_score',
          score: 0.72,
          threshold: 0.6,
          matched_pattern: 'budget_write',
          server_slug: 'campaigns',
          tool_name: 'update_line_items',
        },
      },
    },
  ],
  reviewConfigs: [{ action_name: 'update_line_items', allowed_decisions: ['approve', 'reject', 'edit'] }],
};

export const FIXTURE_CONVERSATIONS = [
  {
    id: 'c1',
    title: 'KW02-03 Gstaad health check',
    lastMessage: 'Nothing needs changing unless you plan to reactivate the flight.',
    timestamp: at(3),
    status: 'active' as const,
    hasActiveTasks: true,
  },
  {
    id: 'c2',
    title: 'Q3 pacing review across all Swiss campaigns',
    lastMessage: 'Six campaigns are behind plan.',
    timestamp: at(120),
    status: 'active' as const,
    hasActiveTasks: false,
  },
  {
    id: 'c3',
    title: 'Creative rejection follow-up',
    lastMessage: 'Slope_Hero_30s was rejected for loudness.',
    timestamp: at(1500),
    status: 'active' as const,
    hasActiveTasks: false,
    embeddedSubAgentId: '7',
  },
];
