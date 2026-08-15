import { describe, test, expect, jest, beforeEach } from '@jest/globals';

/**
 * The recovery loop is the safety net for a turn whose stream dropped. It used
 * to delete the in-flight record unconditionally — even when `handleTask`
 * returned without posting because the task was still non-terminal — and then
 * log "Successfully recovered task". Recovery runs every 5 min over records idle
 * more than 2 min, so any task still working at the 2-minute mark had its only
 * recovery handle destroyed while the user had seen nothing.
 *
 * These tests pin the three outcomes: keep waiting, give up audibly, deliver.
 */

const postMessageMock = jest.fn(async () => ({ ok: true, ts: '999.000' }) as any);

jest.unstable_mockModule('@slack/web-api', () => ({
  WebClient: class {
    chat = {
      postMessage: postMessageMock,
      update: jest.fn(async () => ({ ok: true }) as any),
      delete: jest.fn(async () => ({ ok: true }) as any),
    };
  },
}));

const { recoverOrphanedTasks } = await import('../../src/utils/taskRecovery.js');

const THIRTY_ONE_MIN = 31 * 60 * 1000;

function inFlightRecord(overrides: Partial<any> = {}): any {
  return {
    taskId: 'task-1',
    visitorId: 'T1:U1',
    userId: 'U1',
    teamId: 'T1',
    channelId: 'C1',
    threadTs: '1',
    messageTs: '1',
    statusMessageTs: undefined,
    contextKey: 'T1:C1:1',
    source: 'app_mention',
    appId: 'A1',
    createdAt: Date.now(),
    ...overrides,
  };
}

function harness(task: any, record: any) {
  const store = {
    records: new Map<string, any>([[record.taskId, record]]),
    getAll: jest.fn(async () => Array.from(store.records.values())),
    get: jest.fn(async (id: string) => store.records.get(id) ?? null),
    delete: jest.fn(async (id: string) => {
      store.records.delete(id);
    }),
  };
  const run = () =>
    recoverOrphanedTasks(
      store as any,
      { getTaskStatus: jest.fn(async () => ({ result: task }) as any) } as any,
      { getOrchestratorToken: jest.fn(async () => 'access-token') } as any,
      {
        getByAppId: jest.fn(async () => ({ botToken: 'xoxb-test' })),
        getByTeamId: jest.fn(async () => []),
      } as any,
      { set: jest.fn(async () => undefined) } as any,
      'xoxb-fallback',
      0
    );
  return { store, run };
}

describe('recoverOrphanedTasks', () => {
  beforeEach(() => {
    postMessageMock.mockClear();
  });

  test('keeps the in-flight record when the task is still non-terminal', async () => {
    const task = { id: 'task-1', contextId: 'ctx-1', kind: 'task', status: { state: 'working' } };
    const { store, run } = harness(task, inFlightRecord());

    const stats = await run();

    // The record is the only handle on the turn — it must survive for a later sweep.
    expect(store.delete).not.toHaveBeenCalled();
    expect(store.records.has('task-1')).toBe(true);
    expect(postMessageMock).not.toHaveBeenCalled();
    expect(stats.inProgress).toBe(1);
    expect(stats.recovered).toBe(0);
  });

  test('gives up audibly once the task has been non-terminal for too long', async () => {
    // An orchestrator killed mid-run (OOM) never resumes the task, so the state
    // stays non-terminal forever. Ageing out silently is the exact failure this
    // change exists to remove — the user must be told to resend.
    const task = { id: 'task-1', contextId: 'ctx-1', kind: 'task', status: { state: 'submitted' } };
    const { store, run } = harness(task, inFlightRecord({ createdAt: Date.now() - THIRTY_ONE_MIN }));

    await run();

    expect(postMessageMock).toHaveBeenCalledTimes(1);
    const posted = (postMessageMock.mock.calls[0] as any[])[0];
    expect(posted.channel).toBe('C1');
    expect(String(posted.markdown_text)).toMatch(/send it again/i);
    expect(store.records.has('task-1')).toBe(false);
  });

  test('delivers and cleans up when the task reached a terminal state', async () => {
    const task = {
      id: 'task-1',
      contextId: 'ctx-1',
      kind: 'task',
      status: { state: 'completed' },
      artifacts: [{ artifactId: 'a1', parts: [{ kind: 'text', text: 'the answer' }] }],
    };
    const { store, run } = harness(task, inFlightRecord());

    const stats = await run();

    expect(postMessageMock).toHaveBeenCalled();
    expect(String((postMessageMock.mock.calls[0] as any[])[0].markdown_text)).toContain('the answer');
    expect(store.records.has('task-1')).toBe(false);
    expect(stats.recovered).toBe(1);
  });
});
