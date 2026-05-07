import { WebClient } from '@slack/web-api';
import { Logger } from './logger.js';
import _ from 'lodash';
import { Artifact, DataPart, FileWithBytes, FileWithUri, Task } from '@a2a-js/sdk';

const logger = Logger.getLogger('taskResponseHandler');

/**
 * Get emoji for task state
 */
export function getStateEmoji(state?: Task['status']['state']): string {
  switch (state) {
    case 'completed':
      return '✅';
    case 'working':
    case 'submitted':
      return '⏳';
    case 'input-required':
      return '❓';
    case 'auth-required':
      return '🔐';
    case 'failed':
    case 'rejected':
      return '❌';
    case 'canceled':
      return '🚫';
    default:
      return '📋';
  }
}

/**
 * Get default message for task state
 */
export function getStateMessage(state?: Task['status']['state']): string {
  switch (state) {
    case 'completed':
      return 'Request completed!';
    case 'working':
      return 'Working on your request...';
    case 'submitted':
      return 'Request submitted...';
    case 'input-required':
      return 'I need more information to proceed.';
    case 'auth-required':
      return 'Authentication is required to proceed.';
    case 'failed':
      return 'Task failed. Please try again.';
    case 'rejected':
      return 'Request was rejected.';
    case 'canceled':
      return 'Task was canceled.';
    default:
      return 'Processing...';
  }
}

/**
 * Check if state is a terminated state (no more updates expected)
 * Terminal states: completed, failed, rejected, canceled
 */
export function isTerminatedState(state?: Task['status']['state']): boolean {
  return ['completed', 'failed', 'rejected', 'canceled'].includes(state || '');
}

/**
 * Check if state is an interrupted state (paused, awaiting user action)
 * Interrupted states: input-required, auth-required
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
 * Context for Slack message operations
 */
export interface SlackMessageContext {
  channelId: string;
  threadTs: string;
  messageTs: string; // Original message for reactions
  statusMessageTs?: string; // Status message for updates
}

/**
 * Parameters for handling task response
 */
export interface HandleTaskResponseParams {
  task: Task;
  slackClient: WebClient;
  messageContext: SlackMessageContext;
}

/**
 * Result from handling task response
 */
export interface HandleTaskResponseResult {
  statusMessageTs?: string;
  handled: boolean;
}

/**
 * Check if an error is a Slack `msg_too_long` platform error.
 */
function isMsgTooLongError(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    _.get(err, 'code') === 'slack_webapi_platform_error' &&
    _.get(err, 'data.error') === 'msg_too_long'
  );
}

/**
 * Fallback: upload text as a snippet file when it exceeds Slack's message limit.
 */
async function uploadTextAsSnippet(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  text: string
): Promise<string | undefined> {
  try {
    await slackClient.filesUploadV2({
      channel_id: channelId,
      thread_ts: threadTs,
      content: text,
      filename: 'response.md',
      title: 'Response',
      initial_comment: '📄 The response was too long for a Slack message, so it has been uploaded as a file.',
    });
    logger.info('Uploaded long message as text snippet');
    return undefined;
  } catch (uploadErr) {
    logger.error(uploadErr, `Failed to upload text as snippet: ${uploadErr}`);
    return undefined;
  }
}

export async function postMessage(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  text: string
): Promise<string | undefined> {
  return postOrUpdateMessage(slackClient, channelId, threadTs, text, undefined);
}
/**
 * Post or update a status message.
 * Falls back to uploading a text snippet if the message exceeds Slack's size limit.
 */
export async function postOrUpdateMessage(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  text: string,
  existingTs?: string
): Promise<string | undefined> {
  try {
    if (existingTs) {
      await slackClient.chat.update({
        channel: channelId,
        ts: existingTs,
        markdown_text: text,
      });
      return existingTs;
    } else {
      const result = await slackClient.chat.postMessage({
        channel: channelId,
        thread_ts: threadTs,
        markdown_text: text,
      });
      return result.ts;
    }
  } catch (err) {
    if (isMsgTooLongError(err)) {
      logger.info('Message too long for Slack, uploading as text snippet');
      // If we had a status message, try to update it with a short note
      if (existingTs) {
        await slackClient.chat
          .update({
            channel: channelId,
            ts: existingTs,
            text: '📄 Response uploaded as a file (too long for a message).',
          })
          .catch(() => {});
      }
      return uploadTextAsSnippet(slackClient, channelId, threadTs, text);
    }
    logger.debug({ err, text }, `Failed to post/update message: ${err}`);
    return existingTs;
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
 * Upload file artifacts to Slack
 */
export async function uploadFileArtifacts(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  files: Array<FileWithBytes>
): Promise<void> {
  if (files.length === 0) return;

  logger.info(`Uploading ${files.length} file artifact(s) to Slack`);

  for (const file of files) {
    try {
      await slackClient.filesUploadV2({
        channel_id: channelId,
        thread_ts: threadTs,
        file: Buffer.from(file.bytes, 'base64'),
        filename: file.name,
        initial_comment: file.name,
      });
      logger.debug(`Successfully uploaded file artifact: ${file.name}`);
    } catch (uploadError) {
      logger.error(uploadError, `Failed to upload file artifact ${file.name}: ${uploadError}`);
      // Notify user that file upload failed
      await slackClient.chat
        .postMessage({
          channel: channelId,
          thread_ts: threadTs,
          text: `⚠️ Failed to upload file: ${file.name}`,
        })
        .catch(() => {});
    }
  }
}

/**
 * Handle a complete task response - posts messages and uploads artifacts
 * This is the main entry point for A2A Task -> Slack
 */
export async function handleTask(params: HandleTaskResponseParams): Promise<{ messageTs: string | undefined }> {
  const { task, slackClient, messageContext } = params;

  const { channelId, threadTs, messageTs, statusMessageTs } = messageContext;

  // Check if we should display a message (final or input-required states)
  if (!isInterruptedOrTerminated(task.status.state)) {
    logger.info({ taskId: task.id }, `Task state is still processing, will not post new message: ${task.status.state}`);
    return { messageTs: undefined };
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
  // Update or post the message
  let postedMessageTs: string | undefined;
  if (message) {
    postedMessageTs = await postMessage(slackClient, channelId, threadTs, message);
  }

  // Upload file artifacts
  await uploadFileArtifacts(slackClient, channelId, threadTs, parts.filesWithBytes);

  return { messageTs: postedMessageTs || statusMessageTs || messageTs };
}

/**
 * Handle error case - update reactions and post error message
 */
export async function handleError(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  messageTs: string,
  errorMessage: string = 'An error occurred while processing your request. Please try again.'
): Promise<void> {
  // Remove eyes reaction
  try {
    await slackClient.reactions.remove({
      channel: channelId,
      name: 'eyes',
      timestamp: messageTs,
    });
  } catch (e) {
    // Ignore - reaction may not exist
  }

  await slackClient.chat
    .postMessage({
      channel: channelId,
      thread_ts: threadTs,
      text: `❌ ${errorMessage}`,
    })
    .catch((err) => logger.error(err, `Failed to send error message: ${err}`));
}

/**
 * Build a bug report widget with Confirm/Decline buttons
 */
export interface BugReportWidgetData {
  taskId: string;
  contextId: string;
  reason: string;
  channelId: string;
  threadTs: string;
  actionRequests?: any[];
}

export function buildBugReportWidget(data: BugReportWidgetData): any[] {
  // Encode only the IDs (not the full reason text) to stay within Slack's 2000-char value limit
  const encodedData = Buffer.from(JSON.stringify({
    taskId: data.taskId,
    contextId: data.contextId,
    reason: data.reason.substring(0, 500),
    channelId: data.channelId,
    threadTs: data.threadTs,
    actionRequests: data.actionRequests,
  })).toString('base64');

  return [
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `🐛 *Bug Report*\n\n${data.reason.substring(0, 2000)}\n\nWould you like to confirm this report?`,
      },
    },
    {
      type: 'actions',
      elements: [
        {
          type: 'button',
          text: {
            type: 'plain_text',
            text: '✅ Confirm',
            emoji: true,
          },
          action_id: 'bug_report_confirm',
          value: encodedData,
          style: 'primary',
        },
        {
          type: 'button',
          text: {
            type: 'plain_text',
            text: '❌ Decline',
            emoji: true,
          },
          action_id: 'bug_report_decline',
          value: encodedData,
        },
      ],
    },
  ];
}
