import { beforeEach, describe, test, expect, jest } from '@jest/globals';
import { registerHitlActions } from '../../src/listeners/actions/hitlButton.js';
import { registerInTaskAuthActions } from '../../src/listeners/actions/inTaskAuthButton.js';
import { buildHitlInterruptWidget, buildMultiHitlInterruptWidget } from '../../src/utils/taskResponseHandler.js';
import { buildAuthRequiredWidget, AUTH_ACTION_DONE } from '../../src/utils/inTaskAuth.js';

/**
 * In a channel anyone can click an interrupt card. Only the speaker whose turn
 * raised it may answer: a stranger's click must neither strip the owner's
 * buttons nor reach the orchestrator.
 */

type Handler = (args: any) => Promise<void>;

function fakeApp() {
  const handlers = new Map<string, Handler>();
  const app: any = {
    action: (id: string, h: Handler) => handlers.set(id, h),
    view: () => undefined,
  };
  return { app, handlers };
}

function fakeClient() {
  return {
    chat: {
      postEphemeral: jest.fn(async (_: any) => ({ ok: true })),
      update: jest.fn(async (_: any) => ({ ok: true })),
      postMessage: jest.fn(async (_: any) => ({ ok: true, ts: '1.1' })),
    },
    views: { open: jest.fn(async (_: any) => ({ ok: true })) },
  };
}

function clickBody(clicker: string, value: string) {
  return {
    user: { id: clicker },
    team: { id: 'T1' },
    channel: { id: 'C1' },
    message: { ts: '200.0', thread_ts: '100.0', blocks: [] },
    trigger_id: 'trig',
    actions: [{ value }],
  };
}

/** The button value for *actionId* in a widget's blocks. */
function valueOf(blocks: any[], actionId: string): string {
  for (const b of blocks) {
    for (const e of b.elements ?? []) if (e.action_id === actionId) return e.value;
  }
  throw new Error(`no ${actionId} button`);
}

const hitlData = {
  taskId: 't',
  contextId: 'ctx',
  toolName: 'delete_records',
  reason: 'risky',
  channelId: 'C1',
  threadTs: '100.0',
  actionRequests: [{ name: 'delete_records', args: { target: 'prod' } }],
  ownerUserId: 'U_OWNER',
};

// Nothing may reach the orchestrator on a refused click, so the deps must never be built.
const makeDeps = jest.fn(() => {
  throw new Error('a refused click must not build handler deps');
}) as any;

async function click(handlers: Map<string, Handler>, actionId: string, clicker: string, value: string) {
  const client = fakeClient();
  await handlers.get(actionId)!({ ack: async () => undefined, body: clickBody(clicker, value), client });
  return client;
}

describe('interrupt cards answer only their owner', () => {
  const { app, handlers } = fakeApp();
  registerHitlActions(app, makeDeps);
  registerInTaskAuthActions(app, makeDeps);
  beforeEach(() => makeDeps.mockClear());

  const single = buildHitlInterruptWidget(hitlData);
  const multi = buildMultiHitlInterruptWidget({
    ...hitlData,
    actionRequests: [hitlData.actionRequests[0], { name: 'remove_cache', args: { key: 'k' } }],
  });
  const auth = buildAuthRequiredWidget({
    taskId: 't',
    contextId: 'ctx',
    channelId: 'C1',
    threadTs: '100.0',
    ownerUserId: 'U_OWNER',
  } as any);

  const cases: Array<[string, string]> = [
    ['hitl_approve', valueOf(single, 'hitl_approve')],
    ['hitl_reject', valueOf(single, 'hitl_reject')],
    ['hitl_review_multi', valueOf(multi, 'hitl_review_multi')],
    [AUTH_ACTION_DONE, valueOf(auth, AUTH_ACTION_DONE)],
  ];

  test.each(cases)('%s from another participant is refused and leaves the card alone', async (actionId, value) => {
    const client = await click(handlers, actionId, 'U_STRANGER', value);

    expect(client.chat.postEphemeral).toHaveBeenCalledTimes(1);
    const refusal = client.chat.postEphemeral.mock.calls[0][0];
    expect(refusal).toMatchObject({ channel: 'C1', user: 'U_STRANGER' });
    expect(refusal.text).toContain('<@U_OWNER>');
    expect(client.chat.update).not.toHaveBeenCalled();
    expect(client.views.open).not.toHaveBeenCalled();
    expect(makeDeps).not.toHaveBeenCalled();
  });

  test('the owner still decides: the card records the decision', async () => {
    const client = await click(handlers, 'hitl_reject', 'U_OWNER', valueOf(single, 'hitl_reject'));

    expect(client.chat.postEphemeral).not.toHaveBeenCalled();
    expect(client.chat.update).toHaveBeenCalled();
  });

  test('a card from before owners were recorded behaves as it always did', async () => {
    const legacy = buildHitlInterruptWidget({ ...hitlData, ownerUserId: undefined });
    const client = await click(handlers, 'hitl_reject', 'U_STRANGER', valueOf(legacy, 'hitl_reject'));

    expect(client.chat.postEphemeral).not.toHaveBeenCalled();
    expect(client.chat.update).toHaveBeenCalled();
  });
});
