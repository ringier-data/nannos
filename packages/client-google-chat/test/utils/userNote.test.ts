import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import type { Message } from '@a2a-js/sdk';
import { postUserNote, readUserNote, type PostUserNoteParams } from '../../src/utils/userNote.js';

const ACTIVITY_LOG_EXT = 'urn:nannos:a2a:activity-log:1.0';

function activityMessage(text: string, metadata?: Record<string, unknown>): Message {
  return {
    kind: 'message',
    role: 'agent',
    messageId: 'm1',
    parts: [{ kind: 'text', text }],
    extensions: [ACTIVITY_LOG_EXT],
    metadata,
  } as Message;
}

describe('readUserNote', () => {
  test('returns the text of a note', () => {
    expect(readUserNote(activityMessage(' Looking up campaign 140 now. ', { kind: 'note' }))).toBe(
      'Looking up campaign 140 now.'
    );
  });

  test('ignores an ordinary activity line', () => {
    expect(readUserNote(activityMessage('Using search…', { source: 'orchestrator' }))).toBeUndefined();
    expect(readUserNote(activityMessage('Using search…'))).toBeUndefined();
  });

  test('ignores an empty note', () => {
    expect(readUserNote(activityMessage('   ', { kind: 'note' }))).toBeUndefined();
  });
});

describe('postUserNote', () => {
  let chatService: {
    sendTextMessage: jest.Mock<(p: string, s: string, t: string, threadId?: string) => Promise<{ name?: string }>>;
    updateMessage: jest.Mock<(o: { projectId: string; messageName: string; text: string }) => Promise<unknown>>;
  };
  let inFlightTaskStore: { updateStatusMessageId: jest.Mock<(taskId: string, id: string) => Promise<void>> };

  function params(overrides: Partial<PostUserNoteParams> = {}): PostUserNoteParams {
    return {
      chatService: chatService as unknown as PostUserNoteParams['chatService'],
      inFlightTaskStore: inFlightTaskStore as unknown as PostUserNoteParams['inFlightTaskStore'],
      projectId: 'proj-1',
      spaceId: 'spaces/AAA',
      threadId: 'spaces/AAA/threads/BBB',
      taskId: 'task-1',
      note: 'Checking the budget first.',
      statusMessageId: 'spaces/AAA/messages/STATUS1',
      statusText: 'Thinking... [Using search]',
      ...overrides,
    };
  }

  beforeEach(() => {
    chatService = {
      sendTextMessage: jest.fn<(p: string, s: string, t: string, threadId?: string) => Promise<{ name?: string }>>(),
      updateMessage: jest.fn<(o: { projectId: string; messageName: string; text: string }) => Promise<unknown>>(),
    };
    chatService.sendTextMessage.mockResolvedValue({ name: 'spaces/AAA/messages/STATUS2' });
    chatService.updateMessage.mockResolvedValue({});
    inFlightTaskStore = { updateStatusMessageId: jest.fn<(taskId: string, id: string) => Promise<void>>() };
    inFlightTaskStore.updateStatusMessageId.mockResolvedValue(undefined);
  });

  test('turns the status message into the note and posts a new status message below it', async () => {
    const result = await postUserNote(params());

    expect(chatService.updateMessage).toHaveBeenCalledWith({
      projectId: 'proj-1',
      messageName: 'spaces/AAA/messages/STATUS1',
      text: 'Checking the budget first.',
    });
    expect(chatService.sendTextMessage).toHaveBeenCalledWith(
      'proj-1',
      'spaces/AAA',
      'Thinking... [Using search]',
      'spaces/AAA/threads/BBB'
    );
    expect(chatService.updateMessage.mock.invocationCallOrder[0]).toBeLessThan(
      chatService.sendTextMessage.mock.invocationCallOrder[0]
    );
    expect(inFlightTaskStore.updateStatusMessageId).toHaveBeenCalledWith('task-1', 'spaces/AAA/messages/STATUS2');
    expect(result).toBe('spaces/AAA/messages/STATUS2');
  });

  test('posts the note as a new message when there is no status message', async () => {
    const result = await postUserNote(params({ statusMessageId: undefined }));

    expect(chatService.updateMessage).not.toHaveBeenCalled();
    expect(chatService.sendTextMessage).toHaveBeenCalledTimes(1);
    expect(chatService.sendTextMessage).toHaveBeenCalledWith(
      'proj-1',
      'spaces/AAA',
      'Checking the budget first.',
      'spaces/AAA/threads/BBB'
    );
    expect(result).toBeUndefined();
  });

  test('keeps the status message when the note cannot be shown', async () => {
    chatService.updateMessage.mockRejectedValue(new Error('429'));

    const result = await postUserNote(params());

    expect(chatService.sendTextMessage).not.toHaveBeenCalled();
    expect(inFlightTaskStore.updateStatusMessageId).not.toHaveBeenCalled();
    expect(result).toBe('spaces/AAA/messages/STATUS1');
  });

  test('returns no status message when the new one cannot be posted', async () => {
    chatService.sendTextMessage.mockRejectedValue(new Error('429'));

    const result = await postUserNote(params());

    expect(inFlightTaskStore.updateStatusMessageId).not.toHaveBeenCalled();
    expect(result).toBeUndefined();
  });

  test('does not fail when the note cannot be posted as a new message', async () => {
    chatService.sendTextMessage.mockRejectedValue(new Error('429'));

    await expect(postUserNote(params({ statusMessageId: undefined }))).resolves.toBeUndefined();
  });
});
