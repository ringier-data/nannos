import _ from 'lodash';
import { Logger } from './logger.js';
import { GoogleChatService } from '../services/googleChatService.js';
import { Artifact, DataPart, FileWithBytes, FileWithUri, Task } from '@a2a-js/sdk';
import type { chat_v1 } from 'googleapis';

const logger = Logger.getLogger('taskResponseHandler');

/**
 * Check if state is an interrupted state (paused, awaiting user action)
 */
export function isInterruptedState(state?: Task['status']['state']): boolean {
  return ['input-required', 'auth-required'].includes(state || '');
}

/**
 * Check if state has a user-facing message that should be displayed immediately
 * This includes terminated states plus interrupted states waiting for user action
 */
export function isInterruptedOrTerminated(state?: Task['status']['state']): boolean {
  return ['completed', 'failed', 'rejected', 'canceled', 'input-required', 'auth-required'].includes(state || '');
}

/**
 * Context for Google Chat message operations
 */
export interface ChatMessageContext {
  projectId: string;
  spaceId: string;
  threadId: string;
  messageId: string; // Original message name
  statusMessageId?: string; // Status message name (for updates)
}

/**
 * Parameters for handling task response
 */
export interface HandleTaskResponseParams {
  task: Task;
  chatService: GoogleChatService;
  messageContext: ChatMessageContext;
  /** When true, append 👍/👎 feedback buttons to the response message. */
  includeFeedbackButtons?: boolean;
}

/**
 * Post or update a status message.
 * Falls back to uploading a text snippet if the message exceeds Google Chat's size limit.
 */
export async function postOrUpdateMessage(
  chatService: GoogleChatService,
  projectId: string,
  spaceId: string,
  threadId: string,
  text: string,
  existingMessageId?: string,
  accessoryWidgets?: chat_v1.Schema$AccessoryWidget[],
  cardsV2?: chat_v1.Schema$CardWithId[],
): Promise<string | undefined> {
  try {
    if (existingMessageId) {
      // Update existing message
      await chatService.updateMessage({
        projectId,
        messageName: existingMessageId,
        text,
        accessoryWidgets,
        cardsV2,
      });
      return existingMessageId;
    } else {
      // Post new message in thread
      const result = await chatService.sendMessage({
        projectId,
        spaceId,
        text,
        threadId,
        accessoryWidgets,
        cardsV2,
        messageReplyOption: threadId ? 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD' : undefined,
      });
      return result.name || undefined;
    }
  } catch (err) {
    logger.debug(`Failed to post/update message: ${err}`);
    return existingMessageId;
  }
}

/**
 * Extract text content and file artifacts from A2A artifacts
 */
export function processArtifacts(artifacts?: Artifact[]): {
  textParts: string[];
  filesWithBytes: FileWithBytes[];
  filesWithUri: FileWithUri[];
  dataParts: DataPart[];
} {
  const textParts: string[] = [];
  const filesWithBytes: FileWithBytes[] = [];
  const filesWithUri: FileWithUri[] = [];
  const dataParts: DataPart[] = [];

  if (artifacts) {
    for (const artifact of artifacts) {
      for (const part of artifact.parts) {
        if (part.kind === 'text') {
          textParts.push(part.text);
        } else if (part.kind === 'file') {
          if ('bytes' in part.file && part.file.bytes) {
            filesWithBytes.push(part.file);
          } else if ('uri' in part.file && part.file.uri) {
            filesWithUri.push(part.file);
          } else {
            logger.warn(`Unsupported kind in artifact ${artifact.artifactId}: ${part.kind}`);
          }
        } else if (part.kind === 'data') {
          dataParts.push(part);
        } else {
          logger.warn(`Unsupported part kind: ${_.get(part, 'kind')}`);
        }
      }
    }
  }
  return { textParts, filesWithBytes, filesWithUri, dataParts };
}

/**
 * Handle a complete task response - posts messages and notifies about artifacts
 * Main entry point for handling any A2A response uniformly
 */
export async function handleTask(params: HandleTaskResponseParams): Promise<{ messageId: string | undefined }> {
  const { task, chatService, messageContext, includeFeedbackButtons } = params;

  const { projectId, spaceId, threadId, messageId, statusMessageId } = messageContext;

  // Check if we should display a message (final or input-required states)
  if (!isInterruptedOrTerminated(task.status.state)) {
    logger.info({ taskId: task.id }, `Task state is still processing, will not post new message: ${task.status.state}`);
    return { messageId: undefined };
  }

  // Process artifacts for completed tasks
  const parts = processArtifacts(task.artifacts);

  let message = '';

  // For interrupted states (input-required, auth-required), the authoritative
  // message is in status.message — artifacts are just intermediate streaming
  // tokens from BEFORE the interrupt fired, not the final response.
  if (isInterruptedState(task.status.state) && task.status?.message?.parts) {
    for (const part of task.status.message.parts) {
      if (part.kind === 'text') {
        message += (part as { kind: 'text'; text: string }).text;
      }
    }
  }

  // For terminal states, use artifact text as the message
  if (!message && parts.textParts.length > 0) {
    message = parts.textParts.join('');
  }

  // Final fallback: extract from status message (e.g. completed with no artifacts)
  if (!message && task.status?.message?.parts) {
    for (const part of task.status.message.parts) {
      if (part.kind === 'text') {
        message += (part as { kind: 'text'; text: string }).text;
      }
    }
  }

  const urls = parts.filesWithUri.map((file) => file.uri);
  if (urls.length > 0) {
    message += `\n\nAttached files:\n${urls.join('\n')}`;
  }

  message = message.trim();

  // Build feedback accessory widgets for completed responses
  let accessoryWidgets: chat_v1.Schema$AccessoryWidget[] | undefined;
  let cardsV2: chat_v1.Schema$CardWithId[] | undefined;
  if (includeFeedbackButtons && task.status.state === 'completed' && message) {
    cardsV2 = [
      {
        cardId: 'feedback_card',
        card: {
          sections: [
            {
              widgets: [
                {
                  buttonList: {
                    buttons: [
                      {
                        text: '👍',
                        onClick: {
                          action: {
                            function: 'feedback_positive',
                            parameters: [],
                          },
                        },
                      },
                      {
                        text: '👎',
                        onClick: {
                          action: {
                            function: 'feedback_negative',
                            parameters: [],
                          },
                        },
                      },
                    ],
                  },
                },
              ],
            },
          ],
        },
      },
    ];
  }

  // Update or post the message
  let postedMessageId: string | undefined;
  if (message) {
    postedMessageId = await postOrUpdateMessage(
      chatService,
      projectId,
      spaceId,
      threadId,
      message,
      statusMessageId,
      accessoryWidgets,
      cardsV2,
    );
  }

  // Upload file artifacts
  await chatService.uploadAndSendFileAttachments(projectId, spaceId, threadId, parts.filesWithBytes);

  return { messageId: postedMessageId || statusMessageId || messageId };
}

/**
 * Handle error case - post error message
 */
export async function handleError(
  chatService: GoogleChatService,
  projectId: string,
  spaceId: string,
  threadId: string,
  errorMessage: string = 'An error occurred while processing your request. Please try again.'
): Promise<void> {
  try {
    await chatService.sendTextMessage(projectId, spaceId, `❌ ${errorMessage}`, threadId);
  } catch (err) {
    logger.error(`Failed to send error message: ${err}`);
  }
}

/**
 * Build a bug report card for Google Chat
 */
export interface BugReportCardData {
  taskId: string;
  contextId: string;
  reason: string;
}

export function buildBugReportCard(data: BugReportCardData): chat_v1.Schema$CardWithId {
  return {
    cardId: 'bug_report_card',
    card: {
      header: {
        title: '🐛 Bug Report',
        subtitle: 'Please confirm this bug report',
        imageUrl: 'https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/bug_report/default/48px.svg',
        imageType: 'CIRCLE',
      },
      sections: [
        {
          widgets: [
            {
              textParagraph: {
                text: `<b>Reason:</b>\n${data.reason}`,
              },
            } as any,
            {
              divider: {},
            },
            {
              textParagraph: {
                text: '<b>Would you like to confirm this report?</b>',
              },
            } as any,
            {
              buttonList: {
                buttons: [
                  {
                    text: '✅ Confirm',
                    onClick: {
                      action: {
                        function: 'bug_report_confirm',
                        parameters: [
                          {
                            key: 'taskId',
                            value: data.taskId,
                          },
                          {
                            key: 'contextId',
                            value: data.contextId,
                          },
                          {
                            key: 'reason',
                            value: data.reason,
                          },
                        ],
                      },
                    },
                    color: {
                      red: 0.0,
                      green: 0.54,
                      blue: 0.86,
                      alpha: 1,
                    },
                  },
                  {
                    text: '❌ Decline',
                    onClick: {
                      action: {
                        function: 'bug_report_decline',
                        parameters: [
                          {
                            key: 'taskId',
                            value: data.taskId,
                          },
                        ],
                      },
                    },
                  },
                ],
              },
            } as any,
          ],
        },
      ],
    },
  };
}
