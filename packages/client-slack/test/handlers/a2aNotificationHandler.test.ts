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
  task_state: 'input_required',
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

    // `markdown_text`, as an interactive reply is posted: Slack converts it; `text` would be
    // read as mrkdwn and cannot be combined with it.
    expect(slackClient.chat.postMessage).toHaveBeenCalledWith({
      channel: 'D1',
      markdown_text: 'Sales were up 4%.',
    });
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
      // The run's terminal task state: 'input_required' tells the adopting
      // orchestrator the run asked the user a question and awaits the answer.
      taskState: 'input_required',
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

describe('a run parked on the owner authorization', () => {
  const parkedPayload = {
    scheduler_status: 'auth_required',
    agent_message: 'Nannos needs your permission to use GitHub: https://github.example/authorize',
    user_sub: 'sub-1',
    scheduled_job_id: 10,
    scheduled_job_run_id: 77,
    parked_task_id: 'outer-task-1',
    auth_payload: {
      requires_auth: true,
      auth_requirement: {
        service: 'github',
        auth_methods: [{ method: 'oauth2', auth_url: 'https://github.example/authorize' }],
      },
    },
    reply_to: {
      service: 'console-backend',
      endpoint: 'scheduled_run_resume',
      scheduled_job_id: 10,
      scheduled_job_run_id: 77,
    },
  };

  test('is delivered as the authorization card, with the prose still on the message', async () => {
    const slackClient = mockSlackClient();
    await handleA2ANotification(makeTask(parkedPayload, 'ctx-parked'), botInstallation, {
      userAuthStorage: mockUserAuthStorage(),
      scheduledRunStore: mockScheduledRunStore(),
      slackClientFactory: () => slackClient as unknown as WebClient,
    });

    const posted = (slackClient.chat.postMessage as jest.Mock).mock.calls[0][0] as {
      text: string;
      blocks?: unknown[];
    };
    expect(posted.blocks).toBeDefined();
    // The prose survives alongside the card: it is what a notification digest shows,
    // and it carries the authorize URL for any surface that renders no blocks.
    expect(posted.text).toContain('https://github.example/authorize');
  });

  test('is NOT adoptable: no provenance is recorded for the ask', async () => {
    // ADR-0008 keys an adopted sub-agent's memory by the RUN, and says that holds only
    // because "a run is adoptable exactly once". A parked run delivers twice — the ask
    // now, the result after it is answered — so recording both would put two
    // conversations on one sub-agent thread, which is the case that ADR names as broken.
    const store = mockScheduledRunStore();
    await handleA2ANotification(makeTask(parkedPayload, 'ctx-parked'), botInstallation, {
      userAuthStorage: mockUserAuthStorage(),
      scheduledRunStore: store,
      slackClientFactory: () => mockSlackClient() as unknown as WebClient,
    });

    expect(store.set).not.toHaveBeenCalled();
  });

  test('the result delivered after the answer IS adoptable', async () => {
    const store = mockScheduledRunStore();
    await handleA2ANotification(
      makeTask({ ...parkedPayload, scheduler_status: 'success', auth_payload: undefined }, 'ctx-parked'),
      botInstallation,
      {
        userAuthStorage: mockUserAuthStorage(),
        scheduledRunStore: store,
        slackClientFactory: () => mockSlackClient() as unknown as WebClient,
      }
    );

    expect(store.set).toHaveBeenCalledTimes(1);
  });
});

describe('what the ask says and where its answer lands', () => {
  const base = {
    scheduler_status: 'auth_required',
    agent_message: 'Permission needed: https://gatana.example/begin',
    user_sub: 'sub-1',
    scheduled_job_id: 15,
    scheduled_job_name: 'QA GitHub Identity Check',
    scheduled_job_run_id: 77,
    auth_payload: {
      requires_auth: true,
      auth_requirement: {
        service: 'gateway',
        resource: 'github_get_teams',
        auth_methods: [{ method: 'oauth2', auth_url: 'https://gatana.example/begin' }],
      },
    },
  };

  test('names the tool and the job, not just "permission to continue"', async () => {
    // "Authorization needed for gateway / Nannos needs your permission before it can
    // continue" told the owner neither what was being authorized nor which of their
    // jobs had stopped. `resource` carries the tool; `scheduled_job_name` the job.
    const slackClient = mockSlackClient();
    await handleA2ANotification(makeTask(base, 'ctx-1'), botInstallation, {
      userAuthStorage: mockUserAuthStorage(),
      scheduledRunStore: mockScheduledRunStore(),
      slackClientFactory: () => slackClient as unknown as WebClient,
    });

    const posted = (slackClient.chat.postMessage as jest.Mock).mock.calls[0][0] as { blocks: unknown[] };
    const rendered = JSON.stringify(posted.blocks);
    expect(rendered).toContain('github_get_teams');
    expect(rendered).toContain('QA GitHub Identity Check');
  });

  test('a resumed run replies under the ask that unblocked it', async () => {
    const slackClient = mockSlackClient();
    await handleA2ANotification(
      makeTask(
        {
          ...base,
          scheduler_status: 'success',
          auth_payload: undefined,
          agent_message: 'GitHub connection confirmed.',
          reply_to_message: { channel: 'D1', ts: '111.222' },
        },
        'ctx-1'
      ),
      botInstallation,
      {
        userAuthStorage: mockUserAuthStorage(),
        scheduledRunStore: mockScheduledRunStore(),
        slackClientFactory: () => slackClient as unknown as WebClient,
      }
    );

    const posted = (slackClient.chat.postMessage as jest.Mock).mock.calls[0][0] as { thread_ts?: string };
    expect(posted.thread_ts).toBe('111.222');
  });

  test('an ordinary result is not threaded under someone else channel message', async () => {
    const slackClient = mockSlackClient();
    await handleA2ANotification(
      makeTask({ ...base, scheduler_status: 'success', auth_payload: undefined,
                 reply_to_message: { channel: 'D-OTHER', ts: '999.000' } }, 'ctx-1'),
      botInstallation,
      {
        userAuthStorage: mockUserAuthStorage(),
        scheduledRunStore: mockScheduledRunStore(),
        slackClientFactory: () => slackClient as unknown as WebClient,
      }
    );

    const posted = (slackClient.chat.postMessage as jest.Mock).mock.calls[0][0] as { thread_ts?: string };
    expect(posted.thread_ts).toBeUndefined();
  });
});
