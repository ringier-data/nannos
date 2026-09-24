import type { Message } from '@a2a-js/sdk';
import type { GoogleChatService } from '../services/googleChatService.js';
import type { IInFlightTaskStore } from '../storage/types.js';
import { Logger } from './logger.js';

const logger = Logger.getLogger('userNote');

/**
 * Read a mid-turn note from an activity-log status message.
 *
 * A note is the agent's own words for the user, sent while the turn keeps
 * running (the `notify_user` tool). It rides the activity-log extension with
 * `kind: 'note'` in the message metadata; an ordinary activity line (a tool
 * ran) carries no kind. Returns undefined for anything that is not a note.
 */
export function readUserNote(message?: Message): string | undefined {
  if (message?.metadata?.kind !== 'note') return undefined;
  const text = message.parts
    .map((part) => (part.kind === 'text' ? part.text : ''))
    .join('')
    .trim();
  return text || undefined;
}

export interface PostUserNoteParams {
  chatService: GoogleChatService;
  inFlightTaskStore: IInFlightTaskStore;
  projectId: string;
  spaceId: string;
  threadId: string;
  taskId: string;
  note: string;
  statusMessageId?: string;
  /** Text for the new status message that goes below the note. */
  statusText: string;
}

/**
 * Show a note as an ordinary message in the thread.
 *
 * The final answer replaces the status message in place, so a note posted
 * below the status message would end up under the answer. Instead the status
 * message itself becomes the note, and a new status message goes below it for
 * the rest of the turn.
 *
 * Returns the status message to use from now on. Undefined means there is
 * none, and the final answer is posted as a new message.
 */
export async function postUserNote(params: PostUserNoteParams): Promise<string | undefined> {
  const { chatService, inFlightTaskStore, projectId, spaceId, threadId, taskId, note, statusMessageId, statusText } =
    params;

  if (!statusMessageId) {
    // No status message to turn into the note: it failed to post at the start of the turn.
    await chatService.sendTextMessage(projectId, spaceId, note, threadId).catch((err) => {
      logger.warn(`Failed to post note for task ${taskId}: ${err}`);
    });
    return undefined;
  }

  try {
    await chatService.updateMessage({ projectId, messageName: statusMessageId, text: note });
  } catch (err) {
    // Nothing changed on screen: drop the note and keep the status message.
    logger.warn(`Failed to post note for task ${taskId}: ${err}`);
    return statusMessageId;
  }

  let newStatusMessageId: string | undefined;
  try {
    newStatusMessageId = (await chatService.sendTextMessage(projectId, spaceId, statusText, threadId)).name || undefined;
  } catch (err) {
    // The note is on screen, but no status message is below it.
    logger.warn(`Failed to post status message below note for task ${taskId}: ${err}`);
    return undefined;
  }

  if (newStatusMessageId) {
    // Task recovery must update the new status message, not the note.
    await inFlightTaskStore.updateStatusMessageId(taskId, newStatusMessageId).catch((err) => {
      logger.warn(`Failed to store new status message for task ${taskId}: ${err}`);
    });
  }
  return newStatusMessageId;
}
