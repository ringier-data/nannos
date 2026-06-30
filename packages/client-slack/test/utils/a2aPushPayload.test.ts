import { describe, test, expect } from '@jest/globals';
import { parseA2APushEvent } from '../../src/utils/a2aPushPayload.js';

const schedulerText = '{"scheduler_status":"success","agent_message":"Done","user_sub":"abc"}';

// Protobuf-JSON bodies as produced by the A2A SDK's BasePushNotificationSender:
// MessageToDict(to_stream_response(event)) — a StreamResponse oneof envelope.
const taskEnvelope = {
  task: {
    id: 'task-123',
    contextId: 'ctx-456',
    status: { state: 'TASK_STATE_SUBMITTED' },
  },
};

const statusUpdateEnvelope = {
  statusUpdate: {
    taskId: 'task-123',
    contextId: 'ctx-456',
    status: {
      state: 'TASK_STATE_COMPLETED',
      message: { messageId: 'm1', role: 'ROLE_AGENT', parts: [{ text: schedulerText }] },
    },
  },
};

describe('parseA2APushEvent', () => {
  test('classifies a task envelope and normalizes state', () => {
    const event = parseA2APushEvent(taskEnvelope);
    expect(event.type).toBe('task');
    if (event.type !== 'task') return;
    expect(event.task.kind).toBe('task');
    expect(event.task.id).toBe('task-123');
    expect(event.task.status.state).toBe('submitted');
  });

  test('synthesizes a Task from a terminal statusUpdate, with a readable text part', () => {
    const event = parseA2APushEvent(statusUpdateEnvelope);
    expect(event.type).toBe('task');
    if (event.type !== 'task') return;
    expect(event.task.id).toBe('task-123');
    expect(event.task.contextId).toBe('ctx-456');
    expect(event.task.status.state).toBe('completed');
    const part = event.task.status.message!.parts[0];
    expect(part.kind).toBe('text');
    expect('text' in part && part.kind === 'text' ? part.text : '').toContain('scheduler_status');
  });

  test('marks artifactUpdate and message envelopes as ignored (so they get 200)', () => {
    expect(parseA2APushEvent({ artifactUpdate: { taskId: 't', artifact: {} } })).toEqual({
      type: 'ignored',
      reason: 'artifact-update',
    });
    expect(parseA2APushEvent({ message: { messageId: 'm', parts: [] } })).toEqual({
      type: 'ignored',
      reason: 'message',
    });
  });

  test('passes through an already-JSON-spec Task', () => {
    const jsonSpec = {
      kind: 'task',
      id: 'task-1',
      contextId: 'c',
      status: { state: 'completed', message: { kind: 'message', parts: [{ kind: 'text', text: 'hi' }] } },
    };
    const event = parseA2APushEvent(jsonSpec);
    expect(event.type).toBe('task');
    if (event.type !== 'task') return;
    expect(event.task.status.state).toBe('completed');
    expect(event.task.status.message!.parts[0].kind).toBe('text');
  });

  test('returns invalid for malformed / non-A2A bodies', () => {
    expect(parseA2APushEvent(null).type).toBe('invalid');
    expect(parseA2APushEvent('nope').type).toBe('invalid');
    expect(parseA2APushEvent({}).type).toBe('invalid');
    expect(parseA2APushEvent({ statusUpdate: { taskId: 'x' } }).type).toBe('invalid'); // no status
  });
});
