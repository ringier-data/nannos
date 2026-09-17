import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import type { Task } from '@a2a-js/sdk';
import { handleA2ANotification } from '../../src/handlers/a2aNotificationHandler.js';
import type { HandlerDependencies } from '../../src/handlers/types.js';
import type { IScheduledRunStore, ScheduledRunRecord } from '../../src/storage/types.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const PROJECT_ID = 'proj-1';
const THREAD_NAME = 'spaces/AAA/threads/BBB';

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

function mockChatService(threadName: string | null = THREAD_NAME) {
  return {
    findDirectMessage: jest.fn<(p: string, u: string) => Promise<unknown>>().mockResolvedValue({ name: 'spaces/AAA' }),
    sendTextMessage: jest
      .fn<(p: string, s: string, t: string, threadId?: string) => Promise<unknown>>()
      .mockResolvedValue({ name: 'spaces/AAA/messages/CCC', thread: threadName ? { name: threadName } : undefined }),
  };
}

function mockScheduledRunStore(): IScheduledRunStore {
  return {
    set: jest.fn<(r: ScheduledRunRecord) => Promise<void>>().mockResolvedValue(undefined),
    get: jest.fn<() => Promise<ScheduledRunRecord | null>>().mockResolvedValue(null),
    buildKey: (threadName: string) => threadName,
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
  let chatService: ReturnType<typeof mockChatService>;
  let scheduledRunStore: IScheduledRunStore;

  beforeEach(() => {
    chatService = mockChatService();
    scheduledRunStore = mockScheduledRunStore();
  });

  function deps(): HandlerDependencies {
    return {
      chatService,
      scheduledRunStore,
      userAuthStorage: {
        findByOidcSub: jest.fn<() => Promise<unknown>>().mockResolvedValue({ userId: 'users/123' }),
      },
    } as unknown as HandlerDependencies;
  }

  test('persists provenance keyed by the delivered message thread', async () => {
    await handleA2ANotification(makeTask(schedulerPayload, 'run-ctx-123'), PROJECT_ID, deps());

    // The trailing undefined is the thread to reply in: an ordinary run threads under
    // nothing, because there is no earlier ask of its own to continue.
    expect(chatService.sendTextMessage).toHaveBeenCalledWith(
      PROJECT_ID,
      'spaces/AAA',
      'Sales were up 4%.',
      undefined
    );
    expect(scheduledRunStore.set).toHaveBeenCalledWith({
      contextKey: THREAD_NAME,
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
    await handleA2ANotification(makeTask(schedulerPayload, undefined), PROJECT_ID, deps());

    expect(chatService.sendTextMessage).toHaveBeenCalled();
    expect(scheduledRunStore.set).not.toHaveBeenCalled();
  });

  test('skips provenance when the sent message has no thread name', async () => {
    chatService = mockChatService(null);
    await handleA2ANotification(makeTask(schedulerPayload, 'run-ctx-123'), PROJECT_ID, deps());

    expect(chatService.sendTextMessage).toHaveBeenCalled();
    expect(scheduledRunStore.set).not.toHaveBeenCalled();
  });

  test('still delivers when provenance persistence fails', async () => {
    (scheduledRunStore.set as jest.Mock).mockImplementation(() => Promise.reject(new Error('db down')));

    await expect(
      handleA2ANotification(makeTask(schedulerPayload, 'run-ctx-123'), PROJECT_ID, deps())
    ).resolves.toBeUndefined();
    expect(chatService.sendTextMessage).toHaveBeenCalled();
  });

  test('condition_not_met notifications are dropped before delivery', async () => {
    const payload = { ...schedulerPayload, scheduler_status: 'condition_not_met' };
    await handleA2ANotification(makeTask(payload, 'run-ctx-123'), PROJECT_ID, deps());

    expect(chatService.sendTextMessage).not.toHaveBeenCalled();
    expect(scheduledRunStore.set).not.toHaveBeenCalled();
  });
});


describe('a resumed run threads under the ask that unblocked it', () => {
  /**
   * ADR-0009 calls the chain load-bearing: whatever a resumed run produces — including a
   * SECOND ask, when one authorization leads to another — belongs under the ask the owner
   * answered, so the exchange reads as one conversation rather than loose notices they
   * have to connect themselves.
   *
   * The coordinates are this client's own (a space and a thread). They travel out with the
   * click and come back untouched: console-backend stores `reply_to` opaquely and hands it
   * to whichever client delivers the result.
   */
  let chatService: ReturnType<typeof mockChatService>;

  beforeEach(() => {
    chatService = mockChatService();
  });

  function deps(): HandlerDependencies {
    return {
      chatService,
      scheduledRunStore: mockScheduledRunStore(),
      userAuthStorage: {
        findByOidcSub: jest.fn<() => Promise<unknown>>().mockResolvedValue({ userId: 'users/123' }),
      },
    } as unknown as HandlerDependencies;
  }

  test('the result is posted into the ask thread', async () => {
    const payload = {
      ...schedulerPayload,
      reply_to_message: { space: 'spaces/AAA', thread: 'spaces/AAA/threads/ASK' },
    };

    await handleA2ANotification(makeTask(payload, 'run-ctx-1'), PROJECT_ID, deps());

    expect(chatService.sendTextMessage).toHaveBeenCalledWith(
      PROJECT_ID,
      'spaces/AAA',
      'Sales were up 4%.',
      'spaces/AAA/threads/ASK'
    );
  });

  test('a thread from another space is ignored', async () => {
    // It is this client's own message it threads under; a space that is not the one this
    // notification is going to cannot be that message.
    const payload = {
      ...schedulerPayload,
      reply_to_message: { space: 'spaces/SOMEWHERE-ELSE', thread: 'spaces/SOMEWHERE-ELSE/threads/X' },
    };

    await handleA2ANotification(makeTask(payload, 'run-ctx-2'), PROJECT_ID, deps());

    expect(chatService.sendTextMessage).toHaveBeenCalledWith(
      PROJECT_ID,
      'spaces/AAA',
      'Sales were up 4%.',
      undefined
    );
  });

  test('a run with no ask behind it threads under nothing', async () => {
    await handleA2ANotification(makeTask(schedulerPayload, 'run-ctx-3'), PROJECT_ID, deps());

    expect(chatService.sendTextMessage).toHaveBeenCalledWith(
      PROJECT_ID,
      'spaces/AAA',
      'Sales were up 4%.',
      undefined
    );
  });
});
