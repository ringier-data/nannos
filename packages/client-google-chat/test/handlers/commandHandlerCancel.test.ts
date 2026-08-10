import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { handleAppCommand, AppCommand } from '../../src/handlers/commandHandler.js';
import type { HandlerDependencies } from '../../src/handlers/types.js';
import type { InFlightTask } from '../../src/storage/types.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const THREAD = 'spaces/S1/threads/T1';

function makeTask(overrides: Partial<InFlightTask> = {}): InFlightTask {
  return {
    taskId: 'task-abc-123',
    visitorId: 'P1:users/U1',
    userId: 'users/U1',
    projectId: 'P1',
    spaceId: 'spaces/S1',
    threadId: THREAD,
    messageId: 'spaces/S1/messages/M1',
    contextKey: 'P1:spaces/S1:' + THREAD,
    source: 'space_message',
    createdAt: 1700000000000,
    ttl: 1700003600,
    ...overrides,
  };
}

function makeAppCommand(commandArgument: string): AppCommand {
  return {
    commandArgument,
    spaceId: 'spaces/S1',
    userId: 'users/U1',
    projectId: 'P1',
    threadId: THREAD,
    messageId: 'spaces/S1/messages/M2',
  };
}

interface DepsOptions {
  tasks?: InFlightTask[];
  token?: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  cancelResponse?: any;
}

function makeDeps({
  tasks = [],
  token = 'orch-token',
  cancelResponse = { result: { id: 'task-abc-123', status: { state: 'canceled' } } },
}: DepsOptions = {}) {
  const sendPrivateTextMessage = jest
    .fn<(projectId: string, spaceId: string, userId: string, text: string, threadId: string) => Promise<void>>()
    .mockResolvedValue(undefined);
  const cancelTask = jest
    .fn<(taskId: string, accessToken: string) => Promise<unknown>>()
    .mockResolvedValue(cancelResponse);
  const deleteTask = jest.fn<(taskId: string) => Promise<void>>().mockResolvedValue(undefined);

  const deps = {
    chatService: { sendPrivateTextMessage },
    contextStore: {},
    inFlightTaskStore: {
      getByUser: jest.fn<() => Promise<InFlightTask[]>>().mockResolvedValue(tasks),
      delete: deleteTask,
    },
    userAuthService: {
      getOrchestratorToken: jest.fn<() => Promise<string | null>>().mockResolvedValue(token),
    },
    a2aClientService: { cancelTask },
  } as unknown as HandlerDependencies;

  return { deps, sendPrivateTextMessage, cancelTask, deleteTask };
}

type SendPrivateTextMessageMock = jest.Mock<
  (projectId: string, spaceId: string, userId: string, text: string, threadId: string) => Promise<void>
>;

function lastMessageText(sendPrivateTextMessage: SendPrivateTextMessageMock): string {
  const call = sendPrivateTextMessage.mock.calls.at(-1);
  return call ? call[3] : '';
}

// ---------------------------------------------------------------------------
// handleAppCommand — cancel
// ---------------------------------------------------------------------------

describe('handleAppCommand cancel', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('responds with info when the user has no running tasks', async () => {
    const { deps, sendPrivateTextMessage, cancelTask } = makeDeps({ tasks: [] });

    await handleAppCommand(makeAppCommand('cancel'), deps);

    expect(cancelTask).not.toHaveBeenCalled();
    expect(lastMessageText(sendPrivateTextMessage)).toContain('no running tasks');
  });

  test('cancels the single task in the current thread and confirms', async () => {
    const { deps, sendPrivateTextMessage, cancelTask } = makeDeps({ tasks: [makeTask()] });

    await handleAppCommand(makeAppCommand('cancel'), deps);

    expect(cancelTask).toHaveBeenCalledWith('task-abc-123', 'orch-token');
    expect(lastMessageText(sendPrivateTextMessage)).toContain('Cancellation requested');
    // Reply goes to the thread the command was issued in
    expect(sendPrivateTextMessage).toHaveBeenCalledWith('P1', 'spaces/S1', 'users/U1', expect.any(String), THREAD);
  });

  test('supports the stop alias', async () => {
    const { deps, cancelTask } = makeDeps({ tasks: [makeTask()] });

    await handleAppCommand(makeAppCommand('stop'), deps);

    expect(cancelTask).toHaveBeenCalledWith('task-abc-123', 'orch-token');
  });

  test('falls back to the single task outside the thread', async () => {
    const task = makeTask({ threadId: 'spaces/S1/threads/OTHER' });
    const { deps, cancelTask } = makeDeps({ tasks: [task] });

    await handleAppCommand(makeAppCommand('cancel'), deps);

    expect(cancelTask).toHaveBeenCalledWith('task-abc-123', 'orch-token');
  });

  test('lists tasks when multiple candidates and no task ID given', async () => {
    const tasks = [
      makeTask({ threadId: 'spaces/S1/threads/A' }),
      makeTask({ taskId: 'task-def-456', threadId: 'spaces/S1/threads/B' }),
    ];
    const { deps, sendPrivateTextMessage, cancelTask } = makeDeps({ tasks });

    await handleAppCommand(makeAppCommand('cancel'), deps);

    expect(cancelTask).not.toHaveBeenCalled();
    const text = lastMessageText(sendPrivateTextMessage);
    expect(text).toContain('task-abc-123');
    expect(text).toContain('task-def-456');
  });

  test('cancels the task matching a task ID argument', async () => {
    const tasks = [makeTask(), makeTask({ taskId: 'task-def-456', threadId: 'spaces/S1/threads/B' })];
    const { deps, cancelTask } = makeDeps({
      tasks,
      cancelResponse: { result: { id: 'task-def-456', status: { state: 'canceled' } } },
    });

    await handleAppCommand(makeAppCommand('cancel task-def-456'), deps);

    expect(cancelTask).toHaveBeenCalledWith('task-def-456', 'orch-token');
  });

  test('prompts login when the user has no orchestrator token', async () => {
    const { deps, sendPrivateTextMessage, cancelTask } = makeDeps({ tasks: [makeTask()], token: null });

    await handleAppCommand(makeAppCommand('cancel'), deps);

    expect(cancelTask).not.toHaveBeenCalled();
    expect(lastMessageText(sendPrivateTextMessage)).toContain('log in');
  });

  test('deletes the stale record when the task is no longer cancelable', async () => {
    const { deps, sendPrivateTextMessage, deleteTask } = makeDeps({
      tasks: [makeTask()],
      cancelResponse: { error: { code: -32002, message: 'Task cannot be canceled' } },
    });

    await handleAppCommand(makeAppCommand('cancel'), deps);

    expect(deleteTask).toHaveBeenCalledWith('task-abc-123');
    expect(lastMessageText(sendPrivateTextMessage)).toContain('already finished');
  });

  test('reports other cancel errors without deleting the record', async () => {
    const { deps, sendPrivateTextMessage, deleteTask } = makeDeps({
      tasks: [makeTask()],
      cancelResponse: { error: { code: -32603, message: 'Internal error' } },
    });

    await handleAppCommand(makeAppCommand('cancel'), deps);

    expect(deleteTask).not.toHaveBeenCalled();
    expect(lastMessageText(sendPrivateTextMessage)).toContain('Failed to cancel');
  });
});
