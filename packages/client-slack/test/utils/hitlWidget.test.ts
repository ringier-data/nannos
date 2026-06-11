import { describe, test, expect, jest } from '@jest/globals';
import { WebClient } from '@slack/web-api';
import {
  buildHitlInterruptWidget,
  buildMultiHitlInterruptWidget,
  replaceInterruptWithDecision,
  recordDecision,
  HitlInterruptWidgetData,
} from '../../src/utils/taskResponseHandler.js';

/** Pull the elements of the (single) actions block from a widget's blocks. */
function actionElements(blocks: any[]): any[] {
  return blocks.find((b) => b.type === 'actions')?.elements ?? [];
}
function byAction(blocks: any[], actionId: string): any {
  return actionElements(blocks).find((e) => e.action_id === actionId);
}

const riskScoredAction = {
  name: 'ls',
  args: {
    path: '/',
    _risk_metadata: { source: 'risk_score', score: 1, threshold: 0.8, matched_pattern: 'rm *', call_id: 'c1' },
  },
  description: "Tool 'ls' has risk score 1.00",
};

const baseData: HitlInterruptWidgetData = {
  taskId: 't',
  contextId: 'ctx',
  toolName: 'ls',
  reason: 'risky',
  channelId: 'C1',
  threadTs: '100.0',
};

describe('HITL widget confirm dialogs', () => {
  test('bypass buttons carry a confirm dialog; single-tool approve does not', () => {
    const blocks = buildHitlInterruptWidget({ ...baseData, actionRequests: [riskScoredAction] });

    const bypassTool = byAction(blocks, 'hitl_approve_bypass_tool');
    const bypassPattern = byAction(blocks, 'hitl_approve_bypass_pattern');
    const approve = byAction(blocks, 'hitl_approve');
    const reject = byAction(blocks, 'hitl_reject');

    // High-impact (permanently widens auto-approval) → confirm dialog.
    expect(bypassTool.confirm).toBeDefined();
    expect(bypassTool.confirm.style).toBe('danger');
    expect(bypassTool.confirm.title.text).toContain('Always allow');
    expect(bypassPattern.confirm).toBeDefined();
    expect(bypassPattern.confirm.text.text).toContain('rm *');

    // One-click for the routine single-tool decisions (no confirmation fatigue).
    expect(approve.confirm).toBeUndefined();
    expect(reject.confirm).toBeUndefined();
    expect(reject.style).toBe('danger');
  });

  test('non-risk-scored tools have no bypass buttons and no confirm', () => {
    const plainAction = { name: 'ls', args: { path: '/' }, description: 'list' };
    const blocks = buildHitlInterruptWidget({ ...baseData, actionRequests: [plainAction] });
    expect(byAction(blocks, 'hitl_approve_bypass_tool')).toBeUndefined();
    expect(byAction(blocks, 'hitl_approve').confirm).toBeUndefined();
  });

  test('multi-action "Approve all" carries a confirm dialog; reject/review do not', () => {
    const blocks = buildMultiHitlInterruptWidget({
      ...baseData,
      actionRequests: [riskScoredAction, { ...riskScoredAction, args: { ...riskScoredAction.args, path: '/memories/' } }],
    });
    const approveAll = byAction(blocks, 'hitl_approve');
    expect(approveAll.confirm).toBeDefined();
    expect(approveAll.confirm.title.text).toContain('2 actions');
    expect(byAction(blocks, 'hitl_reject').confirm).toBeUndefined();
    expect(byAction(blocks, 'hitl_review_multi').confirm).toBeUndefined();
    // The blanket payload carries a per-action summary for the post-decision card.
    const decoded = JSON.parse(Buffer.from(approveAll.value, 'base64').toString());
    expect(decoded.summary).toContain('ls');
    expect(decoded.summary).toContain('/memories/');
  });
});

describe('replaceInterruptWithDecision', () => {
  test('updates the widget message with a collapsible task-card decision summary', async () => {
    const update = jest.fn<any>(async () => ({ ok: true }));
    const client = { chat: { update } } as unknown as WebClient;
    await replaceInterruptWithDecision(client, 'C1', '123.456', 'Approved', '`ls`');
    const arg = update.mock.calls[0][0] as any;
    expect(arg.ts).toBe('123.456');
    expect(arg.blocks[0].type).toBe('task_card');
    expect(arg.blocks[0].status).toBe('complete');
    expect(arg.blocks[0].title).toBe('Approved');
    expect(JSON.stringify(arg.blocks[0].details)).toContain('ls');
  });

  test('falls back to a section when task_card is rejected', async () => {
    const update = jest
      .fn<any>()
      .mockRejectedValueOnce(new Error('invalid_blocks'))
      .mockResolvedValue({ ok: true });
    const client = { chat: { update } } as unknown as WebClient;
    await replaceInterruptWithDecision(client, 'C1', '123.456', 'Rejected', '`ls`');
    expect(update).toHaveBeenCalledTimes(2);
    const fallback = update.mock.calls[1][0] as any;
    expect(fallback.blocks[0].type).toBe('section');
    expect(fallback.blocks[0].text.text).toContain('Rejected');
  });
});

describe('recordDecision', () => {
  test('appends a decision card to the open thinking stream and removes the approval message', async () => {
    const appendStream = jest.fn<any>(async () => ({ ok: true }));
    const del = jest.fn<any>(async () => ({ ok: true }));
    const update = jest.fn<any>(async () => ({ ok: true }));
    const client = { chat: { appendStream, delete: del, update } } as unknown as WebClient;

    await recordDecision(client, 'C1', 'approval.ts', 'stream.ts', 'Approved', 'ls /', true);

    const arg = (appendStream.mock.calls[0][0] as any);
    expect(arg.ts).toBe('stream.ts');
    expect(arg.chunks[0].title).toBe('Approved');
    expect(arg.chunks[0].status).toBe('complete');
    expect(arg.chunks[0].details).toBe('ls /');
    // approval widget removed; no standalone-card fallback used
    expect(del).toHaveBeenCalledWith({ channel: 'C1', ts: 'approval.ts' });
    expect(update).not.toHaveBeenCalled();
  });

  test('reject decision card carries error status', async () => {
    const appendStream = jest.fn<any>(async () => ({ ok: true }));
    const client = { chat: { appendStream, delete: jest.fn<any>(async () => ({})) } } as unknown as WebClient;
    await recordDecision(client, 'C1', 'a.ts', 'stream.ts', 'Rejected', 'ls /', false);
    expect((appendStream.mock.calls[0][0] as any).chunks[0].status).toBe('error');
  });

  test('falls back to the standalone decision card when there is no open stream', async () => {
    const appendStream = jest.fn<any>(async () => ({ ok: true }));
    const update = jest.fn<any>(async () => ({ ok: true }));
    const client = { chat: { appendStream, update, delete: jest.fn<any>(async () => ({})) } } as unknown as WebClient;
    await recordDecision(client, 'C1', 'approval.ts', undefined, 'Approved', 'ls /', true);
    expect(appendStream).not.toHaveBeenCalled();
    // replaceInterruptWithDecision path → chat.update on the approval message
    expect((update.mock.calls[0][0] as any).ts).toBe('approval.ts');
  });
});
