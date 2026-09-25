import { describe, test, expect, jest } from '@jest/globals';
import { recoverOrphanedTasks } from '../../src/utils/taskRecovery.js';

/**
 * Recovery is the safety net for a turn whose stream ended before the task did.
 * The live handler leaves the in-flight record in place and shows "Still
 * working" in the status message, so recovery must keep waiting while the task
 * runs, and must replace the status message when it delivers or gives up.
 */

const THIRTY_ONE_MIN = 31 * 60 * 1000;
const GIVE_UP = /could not get the answer/;

function inFlightRecord(overrides: Record<string, unknown> = {}) {
  return {
    taskId: 'task-1',
    visitorId: 'proj-1:users/1',
    userId: 'users/1',
    projectId: 'proj-1',
    spaceId: 'spaces/AAA',
    threadId: 'spaces/AAA/threads/BBB',
    messageId: 'spaces/AAA/messages/REQ',
    statusMessageId: 'spaces/AAA/messages/STATUS' as string | undefined,
    contextKey: 'proj-1:spaces/AAA:spaces/AAA/threads/BBB',
    source: 'space_message',
    createdAt: Date.now(),
    ttl: 0,
    ...overrides,
  };
}

function a2aTask(state: string, text = 'The budget is 5000 CHF.') {
  return {
    kind: 'task',
    id: 'task-1',
    contextId: 'ctx-1',
    status: {
      state,
      message: { kind: 'message', role: 'agent', messageId: 'm1', parts: [{ kind: 'text', text }] },
    },
  };
}

function harness(record: ReturnType<typeof inFlightRecord>, taskStatus: unknown, accessToken: string | null = 'at') {
  const records = new Map([[record.taskId, record]]);
  const store = {
    getAll: jest.fn(async () => Array.from(records.values())),
    get: jest.fn(async (id: string) => records.get(id) ?? null),
    delete: jest.fn(async (id: string) => {
      records.delete(id);
    }),
  };
  const chatService = {
    updateMessage: jest.fn(async (_o: { projectId: string; messageName: string; text: string }) => ({})),
    sendMessage: jest.fn(async (_o: Record<string, unknown>) => ({ name: 'spaces/AAA/messages/NEW' })),
    uploadAndSendFileAttachments: jest.fn(async () => undefined),
  };
  const run = () =>
    recoverOrphanedTasks(
      store as any,
      { getTaskStatus: jest.fn(async () => taskStatus) } as any,
      { getOrchestratorToken: jest.fn(async () => accessToken) } as any,
      chatService as any,
      { set: jest.fn(async () => undefined) } as any,
      0
    );
  return { records, store, chatService, run };
}

describe('recoverOrphanedTasks', () => {
  test('keeps the record of a task that is still running', async () => {
    const h = harness(inFlightRecord(), { result: a2aTask('working') });

    const stats = await h.run();

    expect(stats).toEqual({ recovered: 0, failed: 0, inProgress: 1 });
    expect(h.records.has('task-1')).toBe(true);
    expect(h.chatService.updateMessage).not.toHaveBeenCalled();
    expect(h.chatService.sendMessage).not.toHaveBeenCalled();
  });

  test('gives up on a task that runs past the limit and replaces the status message', async () => {
    const h = harness(inFlightRecord({ createdAt: Date.now() - THIRTY_ONE_MIN }), { result: a2aTask('working') });

    const stats = await h.run();

    expect(stats.failed).toBe(1);
    expect(h.records.has('task-1')).toBe(false);
    expect(h.chatService.updateMessage).toHaveBeenCalledTimes(1);
    const update = h.chatService.updateMessage.mock.calls[0][0];
    expect(update.messageName).toBe('spaces/AAA/messages/STATUS');
    expect(update.text).toMatch(GIVE_UP);
  });

  test('posts the notice as a new message when there is no status message', async () => {
    const h = harness(inFlightRecord({ createdAt: Date.now() - THIRTY_ONE_MIN, statusMessageId: undefined }), {
      result: a2aTask('working'),
    });

    await h.run();

    expect(h.chatService.updateMessage).not.toHaveBeenCalled();
    expect(h.chatService.sendMessage).toHaveBeenCalledTimes(1);
    expect(h.chatService.sendMessage.mock.calls[0][0]).toMatchObject({
      spaceId: 'spaces/AAA',
      threadId: 'spaces/AAA/threads/BBB',
      text: expect.stringMatching(GIVE_UP),
    });
  });

  test('delivers a finished task into the status message', async () => {
    const h = harness(inFlightRecord(), { result: a2aTask('completed') });

    const stats = await h.run();

    expect(stats.recovered).toBe(1);
    expect(h.records.has('task-1')).toBe(false);
    expect(h.chatService.updateMessage).toHaveBeenCalledWith({
      projectId: 'proj-1',
      messageName: 'spaces/AAA/messages/STATUS',
      text: 'The budget is 5000 CHF.',
    });
  });

  test('tells the user when the task cannot be read', async () => {
    const noToken = harness(inFlightRecord(), { result: a2aTask('completed') }, null);
    await noToken.run();
    expect(noToken.records.has('task-1')).toBe(false);
    expect(noToken.chatService.updateMessage.mock.calls[0][0].text).toMatch(GIVE_UP);

    const expired = harness(inFlightRecord(), { error: { code: -32001, message: 'Task not found' } });
    await expired.run();
    expect(expired.records.has('task-1')).toBe(false);
    expect(expired.chatService.updateMessage.mock.calls[0][0].text).toMatch(GIVE_UP);
  });
});
