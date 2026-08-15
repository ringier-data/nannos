import { describe, test, expect } from '@jest/globals';
import { finalizeStreamedTask, isTurnDelivered } from '../../src/utils/taskResponseHandler.js';
import type { Task } from '@a2a-js/sdk';

/**
 * These tests pin the contract that `messageHandler`'s `responsePosted` flag
 * depends on.
 *
 * The regression they guard: the handler used to set `responsePosted = true`
 * unconditionally after calling finalize. When a stream dropped mid-flight the
 * task was still in a non-terminal state, so finalize returned without posting
 * anything — but the flag was set anyway, which deleted the in-flight record.
 * That record is the only handle the recovery loop has, so a recoverable turn
 * became a permanently lost one and the user saw nothing at all.
 */

const messageContext = {
  channelId: 'C1',
  threadTs: '1',
  messageTs: '1',
  statusMessageTs: undefined,
};

/** A streamer that fails the test if finalize touches it. */
function forbiddenStreamer(): any {
  return new Proxy(
    {},
    {
      get(_t, prop) {
        throw new Error(`finalize must not touch the streamer for a non-terminal task (accessed "${String(prop)}")`);
      },
    }
  );
}

/** A Slack client that fails the test if finalize tries to post. */
function forbiddenSlackClient(): any {
  return new Proxy(
    {},
    {
      get(_t, prop) {
        throw new Error(`finalize must not call Slack for a non-terminal task (accessed "${String(prop)}")`);
      },
    }
  );
}

function taskWithState(state: string): Task {
  return {
    id: 'task-1',
    contextId: 'ctx-1',
    kind: 'task',
    status: { state },
  } as unknown as Task;
}

describe('finalizeStreamedTask — non-terminal states', () => {
  // "submitted" is the state observed in the prod incident: the orchestrator was
  // OOMKilled after acknowledging the task but before emitting any status update.
  test.each(['submitted', 'working'])(
    'returns undefined messageTs and posts nothing when the task is still "%s"',
    async (state) => {
      const result = await finalizeStreamedTask({
        task: taskWithState(state),
        streamer: forbiddenStreamer(),
        slackClient: forbiddenSlackClient(),
        messageContext,
      });

      // undefined is the signal the handler uses to keep the in-flight record so
      // the recovery loop can still finish the turn.
      expect(result.messageTs).toBeUndefined();
    }
  );

});

describe('isTurnDelivered', () => {
  const call = (o: Partial<Parameters<typeof isTurnDelivered>[0]>) =>
    isTurnDelivered({
      finalizeMessageTs: undefined,
      hasStreamedAnswer: false,
      streamErrored: false,
      ...o,
    });

  test('finalize posted → delivered', () => {
    expect(call({ finalizeMessageTs: '123.456' })).toBe(true);
  });

  test('clean close after a streamed answer → delivered (no duplicate re-post)', () => {
    // A stream can close right after the final artifact without ever sending a
    // terminal status-update. The user has the answer; keeping the record would
    // let recovery post it a second time.
    expect(call({ hasStreamedAnswer: true })).toBe(true);
  });

  test('stream errored mid-answer → NOT delivered', () => {
    // hasAnswer flips on the FIRST chunk, so a mid-answer failure leaves a
    // truncated reply on screen. Treating that as delivered would drop the
    // in-flight record, strand the missing tail, and break the promise made by
    // the notice on the sealed widget.
    expect(call({ hasStreamedAnswer: true, streamErrored: true })).toBe(false);
  });

  test('stream errored before any answer → NOT delivered', () => {
    expect(call({ streamErrored: true })).toBe(false);
  });

  test('nothing happened → NOT delivered', () => {
    expect(call({})).toBe(false);
  });

  test('finalize wins even if the stream errored', () => {
    // finalize only posts for interrupted/terminal tasks, so a defined ts means
    // the authoritative answer reached Slack regardless of how the stream ended.
    expect(call({ finalizeMessageTs: '123.456', streamErrored: true, hasStreamedAnswer: true })).toBe(true);
  });
});
