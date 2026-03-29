import { WebClient } from '@slack/web-api';
import { Logger } from './logger.js';
import { base64ToBuffer, getExtensionFromMimeType } from './fileUtils.js';
import { A2AResponse, A2AArtifact } from '../services/a2aClientService.js';
import type { IContextStore, IInFlightTaskStore, InFlightTask } from '../storage/types.js';

const logger = Logger.getLogger('taskResponseHandler');

/**
 * Task state type alias for clarity
 */
export type TaskState = A2AResponse['state'];

/**
 * Get emoji for task state
 */
export function getStateEmoji(state?: TaskState): string {
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
export function getStateMessage(state?: TaskState): string {
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
export function isTerminatedState(state?: TaskState): boolean {
  return ['completed', 'failed', 'rejected', 'canceled'].includes(state || '');
}

/**
 * Check if state is an interrupted state (paused, awaiting user action)
 * Interrupted states: input-required, auth-required
 */
export function isInterruptedState(state?: TaskState): boolean {
  return ['input-required', 'auth-required'].includes(state || '');
}

/**
 * Check if state has a user-facing message that should be displayed immediately
 * This includes terminated states plus interrupted states waiting for user action
 */
export function shouldDisplayMessage(state?: TaskState): boolean {
  return ['completed', 'failed', 'rejected', 'canceled', 'input-required', 'auth-required'].includes(state || '');
}

/**
 * Format a status message with emoji
 */
export function formatStatusMessage(state?: TaskState, message?: string, includeEmojiForCompleted = false): string {
  const emoji = getStateEmoji(state);
  const text = message || getStateMessage(state);

  // For completed state, only prepend emoji if explicitly requested
  if (state === 'completed' && !includeEmojiForCompleted) {
    return text;
  }

  return `${emoji} ${text}`;
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
  response: A2AResponse;
  slackClient: WebClient;
  messageContext: SlackMessageContext;
  contextKey: string;
  contextStore: IContextStore;
  inFlightTaskStore?: IInFlightTaskStore;
  webhookToken?: string;
  source: 'app_mention' | 'direct_message';
  userId: string;
  teamId: string;
  appId?: string;
}

/**
 * Result from handling task response
 */
export interface HandleTaskResponseResult {
  statusMessageTs?: string;
  handled: boolean;
}

/**
 * Post or update a status message
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
        text,
      });
      return existingTs;
    } else {
      const result = await slackClient.chat.postMessage({
        channel: channelId,
        thread_ts: threadTs,
        text,
      });
      return result.ts;
    }
  } catch (err) {
    logger.debug({ err, text }, `Failed to post/update message: ${err}`);
    return existingTs;
  }
}

/**
 * Collected file artifact ready for upload
 */
export interface FileArtifact {
  data: Buffer;
  filename: string;
  mimeType: string;
}

/**
 * Extract text content and file artifacts from A2A artifacts
 */
export function processArtifacts(artifacts?: A2AArtifact[]): {
  textParts: string[];
  fileArtifacts: FileArtifact[];
} {
  const textParts: string[] = [];
  const fileArtifacts: FileArtifact[] = [];

  if (!artifacts?.length) {
    return { textParts, fileArtifacts };
  }

  for (const artifact of artifacts) {
    for (const part of artifact.parts) {
      if (part.kind === 'text' && part.text) {
        textParts.push(part.text);
      } else if ((part.kind === 'data' || part.kind === 'file') && part.data && part.mimeType) {
        const buffer = base64ToBuffer(part.data);
        const extension = getExtensionFromMimeType(part.mimeType);
        const filename = part.name || artifact.name || `artifact-${artifact.artifactId}.${extension}`;
        fileArtifacts.push({ data: buffer, filename, mimeType: part.mimeType });
        logger.info(`Collected file artifact: ${filename} (${part.mimeType}, ${buffer.length} bytes)`);
      }
    }
  }

  return { textParts, fileArtifacts };
}

/**
 * Upload file artifacts to Slack
 */
export async function uploadFileArtifacts(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  fileArtifacts: FileArtifact[]
): Promise<void> {
  if (fileArtifacts.length === 0) return;

  logger.info(`Uploading ${fileArtifacts.length} file artifact(s) to Slack`);

  for (const file of fileArtifacts) {
    try {
      await slackClient.filesUploadV2({
        channel_id: channelId,
        thread_ts: threadTs,
        file: file.data,
        filename: file.filename,
        initial_comment: `📎 ${file.filename}`,
      });
      logger.info(`Successfully uploaded file artifact: ${file.filename}`);
    } catch (uploadError) {
      logger.error(uploadError, `Failed to upload file artifact ${file.filename}: ${uploadError}`);
      // Notify user that file upload failed
      await slackClient.chat
        .postMessage({
          channel: channelId,
          thread_ts: threadTs,
          text: `⚠️ Failed to upload file: ${file.filename}`,
        })
        .catch(() => {});
    }
  }
}

/**
 * Handle a complete task response - posts messages and uploads artifacts
 * This is the main entry point for handling any A2A response uniformly
 */
export async function handleTaskResponse(params: HandleTaskResponseParams): Promise<HandleTaskResponseResult> {
  const {
    response,
    slackClient,
    messageContext,
    contextKey,
    contextStore,
    inFlightTaskStore,
    webhookToken,
    source,
    userId,
    teamId,
    appId,
  } = params;

  const { channelId, threadTs, messageTs } = messageContext;
  let { statusMessageTs } = messageContext;

  // Check if we should display a message (final or input-required states)
  if (shouldDisplayMessage(response.state)) {
    logger.info(`Handling displayable response: taskId=${response.taskId}, state=${response.state}`);

    // Store context ID and last processed timestamp for conversation continuity
    if (response.contextId) {
      await contextStore.set(contextKey, response.contextId, messageTs);
    }

    // Process artifacts for completed tasks
    let message = response.message || getStateMessage(response.state);
    const { textParts, fileArtifacts } = processArtifacts(response.artifacts);

    // If we have text artifacts, use them as the message
    if (textParts.length > 0) {
      message = textParts.join('\n\n');
    }

    // Add error info if present
    if (response.error) {
      message = `${message}\n\nError: ${response.error}`;
    }

    // Format the final message
    const displayMessage = formatStatusMessage(response.state, message);

    // Update or post the message
    statusMessageTs = await postOrUpdateMessage(slackClient, channelId, threadTs, displayMessage, statusMessageTs);

    // Upload file artifacts
    await uploadFileArtifacts(slackClient, channelId, threadTs, fileArtifacts);

    // For non-terminated states that need user input, store task for potential webhook callback
    if (!isTerminatedState(response.state) && response.taskId && inFlightTaskStore) {
      logger.info(`Task ${response.taskId} awaiting user input (state: ${response.state}), storing for webhook`);
      await inFlightTaskStore.save({
        taskId: response.taskId,
        visitorId: inFlightTaskStore.buildVisitorId(teamId, userId),
        userId,
        teamId,
        channelId,
        threadTs,
        messageTs,
        statusMessageTs,
        contextKey,
        webhookToken,
        source,
        appId,
        createdAt: Date.now(),
      });
    }

    return { statusMessageTs, handled: true };
  }

  // Task still in progress - store for webhook callback without displaying message yet
  if (response.taskId && inFlightTaskStore) {
    logger.info(`Task ${response.taskId} still working (state: ${response.state}), storing for webhook callback`);

    await inFlightTaskStore.save({
      taskId: response.taskId,
      visitorId: inFlightTaskStore.buildVisitorId(teamId, userId),
      userId,
      teamId,
      channelId,
      threadTs,
      messageTs,
      statusMessageTs,
      contextKey,
      webhookToken,
      source,
      appId,
      createdAt: Date.now(),
    });

    // Store context ID if we got one
    if (response.contextId) {
      await contextStore.set(contextKey, response.contextId, messageTs);
    }

    return { statusMessageTs, handled: true };
  }

  // Immediate failure with no task ID
  if (!response.success) {
    logger.error(`A2A request failed: ${response.error}`);

    const errorMessage = `❌ ${response.error || 'Failed to process your request.'}`;
    statusMessageTs = await postOrUpdateMessage(slackClient, channelId, threadTs, errorMessage, statusMessageTs);

    return { statusMessageTs, handled: true };
  }

  return { statusMessageTs, handled: false };
}

/**
 * Handle streaming status update events
 * Posts a new message for each status update to preserve history
 */
export async function handleStreamStatusUpdate(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  state: TaskState,
  message?: string,
  existingStatusTs?: string
): Promise<string | undefined> {
  if (!message) return existingStatusTs;

  const statusText = formatStatusMessage(state, message, true);

  // Always post a new message to preserve history (don't update existing)
  try {
    const result = await slackClient.chat.postMessage({
      channel: channelId,
      thread_ts: threadTs,
      text: statusText,
    });

    return result.ts;
  } catch (error) {
    logger.debug(`Failed to post status message: ${error}`);
    return existingStatusTs;
  }
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
 * Handle webhook callback from A2A server
 * This processes the final result from an async task
 */
export async function handleWebhookCallback(
  payload: {
    taskId: string;
    contextId?: string;
    state: TaskState;
    message?: string;
    artifacts?: A2AArtifact[];
    error?: string;
    token?: string;
  },
  task: InFlightTask,
  slackClient: WebClient,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore
): Promise<{ success: boolean; message: string }> {
  const { taskId } = payload;

  logger.info(`Processing webhook callback for task ${taskId}`);

  // Validate webhook token if we stored one
  if (task.webhookToken && payload.token !== task.webhookToken) {
    logger.warn(`Invalid webhook token for task ${taskId}. Expected: ${task.webhookToken}, Got: ${payload.token}`);
    return { success: false, message: 'Invalid webhook token' };
  }

  try {
    // Process artifacts
    let message = payload.message || getStateMessage(payload.state);
    const { textParts, fileArtifacts } = processArtifacts(payload.artifacts);

    if (textParts.length > 0) {
      message = textParts.join('\n\n');
    }

    if (payload.error) {
      message = `${message}\n\nError: ${payload.error}`;
    }

    const fullMessage = formatStatusMessage(payload.state, message);

    // Update or post message
    await postOrUpdateMessage(slackClient, task.channelId, task.threadTs, fullMessage, task.statusMessageTs);

    // Upload file artifacts
    await uploadFileArtifacts(slackClient, task.channelId, task.threadTs, fileArtifacts);

    // Store context ID for conversation continuity
    if (payload.contextId) {
      await contextStore.set(task.contextKey, payload.contextId, task.messageTs);
    }

    // Clean up - delete the in-flight task record
    await inFlightTaskStore.delete(taskId);

    logger.info(`Successfully processed webhook callback for task ${taskId}`);
    return { success: true, message: 'Webhook processed successfully' };
  } catch (error) {
    logger.error(error, `Error processing webhook for task ${taskId}: ${error}`);

    // Try to notify user of error
    try {
      await slackClient.chat.postMessage({
        channel: task.channelId,
        thread_ts: task.threadTs,
        text: '❌ An error occurred while processing the response. Please try again.',
      });
    } catch (e) {
      // Ignore notification errors
    }

    return { success: false, message: `Error processing webhook: ${error}` };
  }
}

/**
 * Handle status update from webhook (intermediate updates)
 * Posts new messages to preserve history
 */
export async function handleWebhookStatusUpdate(
  payload: { taskId: string; state: TaskState; message?: string },
  task: InFlightTask,
  slackClient: WebClient,
  inFlightTaskStore: IInFlightTaskStore
): Promise<void> {
  const { taskId, state, message } = payload;

  logger.debug(`Processing status update for task ${taskId}: ${state}`);

  const statusMessage = formatStatusMessage(state, message, true);

  try {
    // Always post a new message to preserve history
    const result = await slackClient.chat.postMessage({
      channel: task.channelId,
      thread_ts: task.threadTs,
      text: statusMessage,
    });

    if (result.ts) {
      await inFlightTaskStore.updateStatusMessageTs(taskId, result.ts);
    }
  } catch (error) {
    logger.debug(`Failed to post Slack status message: ${error}`);
  }
}
