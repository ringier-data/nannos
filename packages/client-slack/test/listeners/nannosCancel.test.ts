import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { handleCancelSubcommand } from '../../src/listeners/commands/nannos.js';
import { UserAuthService } from '../../src/services/userAuthService.js';
import { A2AClientService } from '../../src/services/a2aClientService.js';
import type { IInFlightTaskStore, InFlightTask } from '../../src/storage/types.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeTask(overrides: Partial<InFlightTask> = {}): InFlightTask {
  return {
    taskId: 'task-abc-123',
    visitorId: 'T1:U1',
    userId: 'U1',
    teamId: 'T1',
    channelId: 'C1',
    threadTs: '1700000000.000100',
    messageTs: '1700000000.000100',
    contextKey: 'T1:C1:1700000000.000100',
    source: 'app_mention',
    createdAt: 1700000000000,
    ttl: 1700003600,
    ...overrides,
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makeArgs(respond: jest.Mock): any {
  return {
    command: { command: '/nannos', user_id: 'U1', team_id: 'T1', channel_id: 'C1', text: 'cancel' },
    respond,
  };
}

function makeStore(tasks: InFlightTask[]) {
  return {
    getByUser: jest.fn<() => Promise<InFlightTask[]>>().mockResolvedValue(tasks),
    delete: jest.fn<() => Promise<void>>().mockResolvedValue(undefined),
  } as unknown as IInFlightTaskStore;
}

function makeAuth(token: string | null = 'orch-token') {
  return {
    getOrchestratorToken: jest.fn<() => Promise<string | null>>().mockResolvedValue(token),
  } as unknown as UserAuthService;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makeA2A(response: any = { result: { id: 'task-abc-123', status: { state: 'canceled' } } }) {
  return {
    cancelTask: jest.fn<() => Promise<unknown>>().mockResolvedValue(response),
  } as unknown as A2AClientService;
}

// ---------------------------------------------------------------------------
// handleCancelSubcommand
// ---------------------------------------------------------------------------

describe('handleCancelSubcommand', () => {
  let respond: jest.Mock;

  beforeEach(() => {
    respond = jest.fn<() => Promise<void>>().mockResolvedValue(undefined) as jest.Mock;
  });

  test('responds with info when the user has no running tasks', async () => {
    const a2a = makeA2A();

    await handleCancelSubcommand(makeArgs(respond), makeAuth(), a2a, makeStore([]), '');

    expect(respond).toHaveBeenCalledWith(expect.objectContaining({ text: expect.stringContaining('no running tasks') }));
    expect(a2a.cancelTask).not.toHaveBeenCalled();
  });

  test('cancels the single running task and confirms', async () => {
    const a2a = makeA2A();
    const store = makeStore([makeTask()]);

    await handleCancelSubcommand(makeArgs(respond), makeAuth(), a2a, store, '');

    expect(a2a.cancelTask).toHaveBeenCalledWith('task-abc-123', 'orch-token');
    expect(respond).toHaveBeenCalledWith(
      expect.objectContaining({ text: expect.stringContaining('Cancellation requested') })
    );
  });

  test('lists tasks and does not cancel when multiple tasks and no task ID given', async () => {
    const a2a = makeA2A();
    const store = makeStore([makeTask(), makeTask({ taskId: 'task-def-456', threadTs: '1700000100.000200' })]);

    await handleCancelSubcommand(makeArgs(respond), makeAuth(), a2a, store, '');

    expect(a2a.cancelTask).not.toHaveBeenCalled();
    const text = (respond.mock.calls[0][0] as { text: string }).text;
    expect(text).toContain('task-abc-123');
    expect(text).toContain('task-def-456');
    expect(text).toContain('cancel <task_id>');
  });

  test('cancels the task matching a task ID prefix argument', async () => {
    const a2a = makeA2A({ result: { id: 'task-def-456', status: { state: 'canceled' } } });
    const store = makeStore([makeTask(), makeTask({ taskId: 'task-def-456' })]);

    await handleCancelSubcommand(makeArgs(respond), makeAuth(), a2a, store, 'task-def');

    expect(a2a.cancelTask).toHaveBeenCalledWith('task-def-456', 'orch-token');
  });

  test('responds with not-found when the task ID argument matches nothing', async () => {
    const a2a = makeA2A();
    const store = makeStore([makeTask()]);

    await handleCancelSubcommand(makeArgs(respond), makeAuth(), a2a, store, 'nope');

    expect(a2a.cancelTask).not.toHaveBeenCalled();
    expect(respond).toHaveBeenCalledWith(
      expect.objectContaining({ text: expect.stringContaining('No running task matches') })
    );
  });

  test('prompts login when the user has no orchestrator token', async () => {
    const a2a = makeA2A();

    await handleCancelSubcommand(makeArgs(respond), makeAuth(null), a2a, makeStore([makeTask()]), '');

    expect(a2a.cancelTask).not.toHaveBeenCalled();
    expect(respond).toHaveBeenCalledWith(expect.objectContaining({ text: expect.stringContaining('log in') }));
  });

  test('deletes the stale record when the task is no longer cancelable', async () => {
    const a2a = makeA2A({ error: { code: -32002, message: 'Task cannot be canceled' } });
    const store = makeStore([makeTask()]);

    await handleCancelSubcommand(makeArgs(respond), makeAuth(), a2a, store, '');

    expect(store.delete).toHaveBeenCalledWith('task-abc-123');
    expect(respond).toHaveBeenCalledWith(expect.objectContaining({ text: expect.stringContaining('already finished') }));
  });

  test('reports other cancel errors without deleting the record', async () => {
    const a2a = makeA2A({ error: { code: -32603, message: 'Internal error' } });
    const store = makeStore([makeTask()]);

    await handleCancelSubcommand(makeArgs(respond), makeAuth(), a2a, store, '');

    expect(store.delete).not.toHaveBeenCalled();
    expect(respond).toHaveBeenCalledWith(expect.objectContaining({ text: expect.stringContaining('Failed to cancel') }));
  });
});
