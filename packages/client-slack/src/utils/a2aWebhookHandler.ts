import { WebClient } from '@slack/web-api';
import { Logger } from './logger.js';
import type { IInFlightTaskStore, IContextStore } from '../storage/types.js';
import { A2AResponse, A2APart } from '../services/a2aClientService.js';
import { handleWebhookCallback, handleWebhookStatusUpdate } from './taskResponseHandler.js';

// Re-export for backwards compatibility
export { isTerminatedState } from './taskResponseHandler.js';

const logger = Logger.getLogger('a2aWebhookHandler');

/**
 * A2A webhook callback payload structure
 * This is what the A2A server sends when a task completes (per A2A protocol)
 */
export interface A2AWebhookPayload {
  taskId: string;
  contextId?: string;
  state: A2AResponse['state'];
  message?: string;
  artifacts?: Array<{
    artifactId: string;
    name?: string;
    parts: A2APart[];
  }>;
  error?: string;
  token?: string; // Token provided by client during task submission for webhook validation
  timestamp?: string; // ISO timestamp of the notification
  kind?: string; // Type of notification payload (e.g., 'task')
}

/**
 * Handle A2A webhook callback
 * Called when A2A server completes processing and sends result to our webhook
 * Per A2A protocol, the server includes the token we provided for validation
 */
export async function handleA2AWebhook(
  payload: A2AWebhookPayload,
  slackClient: WebClient,
  inFlightTaskStore: IInFlightTaskStore,
  contextStore: IContextStore
): Promise<{ success: boolean; message: string }> {
  const { taskId } = payload;

  logger.info(`Received A2A webhook callback for task ${taskId}`);
  logger.debug({ payload }, 'Webhook payload');

  // Retrieve task context from DynamoDB
  const task = await inFlightTaskStore.get(taskId);

  if (!task) {
    logger.info(`No in-flight task found for taskId ${taskId} (may have already completed)`);
    return { success: false, message: 'Task not found' };
  }

  // Delegate to centralized handler
  return handleWebhookCallback(payload, task, slackClient, contextStore, inFlightTaskStore);
}

/**
 * Handle status update webhook (intermediate updates)
 */
export async function handleA2AStatusUpdate(
  payload: { taskId: string; state: A2AResponse['state']; message?: string },
  slackClient: WebClient,
  inFlightTaskStore: IInFlightTaskStore
): Promise<void> {
  const { taskId, state, message } = payload;

  logger.debug(`Received status update for task ${taskId}: ${state}`);

  const task = await inFlightTaskStore.get(taskId);
  if (!task) {
    logger.debug(`No in-flight task found for status update: ${taskId}`);
    return;
  }

  // Delegate to centralized handler
  await handleWebhookStatusUpdate({ taskId, state, message }, task, slackClient, inFlightTaskStore);
}
