import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import type { WebClient } from '@slack/web-api';
import type { Task } from '@a2a-js/sdk';
import { handleA2ANotification } from '../../src/handlers/a2aNotificationHandler.js';
import type { BotInstallation, IScheduledRunStore, IUserAuthStorage, ScheduledRunRecord } from '../../src/storage/types.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const botInstallation = {
  teamId: 'T1',
  botName: 'nannos',
  botToken: 'xoxb-token',
} as unknown as BotInstallation;

function makeTask(payload: Record<string, unknown>, contextId?: string): Task {
  return {
    kind: 'task',
    id: 'task-1',
    contextId,
    status: {
      state: 'completed',
      message: {
        kind: 'message',
        role: 'agent',
        messageId: 'm1',
        parts: [{ kind: 'text', text: JSON.stringify(payload) }],
      },
    },
  } as unknown as Task;
}

function mockUserAuthStorage(found = true): IUserAuthStorage {
  return {
    findByOidcSubAndTeam: jest.fn<() => Promise<unknown>>().mockResolvedValue(found ? { userId: 'U1' } : null),
  } as unknown as IUserAuthStorage;
}

function mockSlackClient(postTs: string | undefined = '111.222') {
  return {
    conversations: {
      open: jest.fn<(args: unknown) => Promise<unknown>>().mockResolvedValue({ ok: true, channel: { id: 'D1' } }),
    },
    chat: {
      postMessage: jest.fn<(args: unknown) => Promise<unknown>>().mockResolvedValue({ ok: true, ts: postTs }),
    },
  };
}

function mockScheduledRunStore(): IScheduledRunStore {
  return {
    set: jest.fn<(r: ScheduledRunRecord) => Promise<void>>().mockResolvedValue(undefined),
    get: jest.fn<() => Promise<ScheduledRunRecord | null>>().mockResolvedValue(null),
    buildKey: (channelId: string, messageTs: string) => `${channelId}:${messageTs}`,
  };
}

const schedulerPayload = {
  scheduler_status: 'success',
  agent_message: 'Sales were up 4%.',
  user_sub: 'oidc-sub-1',
  scheduled_job_id: 7,
  scheduled_job_run_id: 42,
  sub_agent_id: 5,
  sub_agent_name: 'Report Agent',
  prompt: "Summarize yesterday's sales.",
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('handleA2ANotification scheduled-run provenance', () => {
  let slackClient: ReturnType<typeof mockSlackClient>;
  let scheduledRunStore: IScheduledRunStore;

  beforeEach(() => {
    slackClient = mockSlackClient();
    scheduledRunStore = mockScheduledRunStore();
  });

  function deps() {
    return {
      userAuthStorage: mockUserAuthStorage(),
      scheduledRunStore,
      slackClientFactory: () => slackClient as unknown as WebClient,
    };
  }

  test('persists provenance keyed by the delivered message ts', async () => {
    await handleA2ANotification(makeTask(schedulerPayload, 'run-ctx-123'), botInstallation, deps());

    expect(slackClient.chat.postMessage).toHaveBeenCalledWith({ channel: 'D1', text: 'Sales were up 4%.' });
    expect(scheduledRunStore.set).toHaveBeenCalledWith({
      contextKey: 'D1:111.222',
      contextId: 'run-ctx-123',
      scheduledJobId: 7,
      scheduledJobRunId: 42,
      subAgentId: 5,
      subAgentName: 'Report Agent',
      prompt: "Summarize yesterday's sales.",
      resultSummary: 'Sales were up 4%.',
      schedulerStatus: 'success',
      errorMessage: undefined,
    });
  });

  test('skips provenance when the task has no contextId', async () => {
    await handleA2ANotification(makeTask(schedulerPayload, undefined), botInstallation, deps());

    expect(slackClient.chat.postMessage).toHaveBeenCalled();
    expect(scheduledRunStore.set).not.toHaveBeenCalled();
  });

  test('still delivers when provenance persistence fails', async () => {
    (scheduledRunStore.set as jest.Mock).mockImplementation(() => Promise.reject(new Error('db down')));

    await expect(
      handleA2ANotification(makeTask(schedulerPayload, 'run-ctx-123'), botInstallation, deps())
    ).resolves.toBeUndefined();
    expect(slackClient.chat.postMessage).toHaveBeenCalled();
  });

  test('condition_not_met notifications are dropped before delivery', async () => {
    const payload = { ...schedulerPayload, scheduler_status: 'condition_not_met' };
    await handleA2ANotification(makeTask(payload, 'run-ctx-123'), botInstallation, deps());

    expect(slackClient.chat.postMessage).not.toHaveBeenCalled();
    expect(scheduledRunStore.set).not.toHaveBeenCalled();
  });
});
