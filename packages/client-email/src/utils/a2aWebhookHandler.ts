import { Logger } from './logger.js';
import { Storage } from '../storage/storage.js';
import { EmailOutboundService } from '../services/emailOutboundService.js';
import type { A2AResponse, A2AArtifact, A2APart } from '../services/a2aClientService.js';
// import { base64ToBuffer, getExtensionFromMimeType } from './fileUtils.js';

const logger = Logger.getLogger('a2aWebhookHandler');

/**
 * A2A webhook callback payload structure.
 * Sent by the A2A server when a task completes.
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
  token?: string;
  timestamp?: string;
  kind?: string;
}

type TaskState = A2AResponse['state'];

function isFinalState(state?: TaskState): boolean {
  return ['completed', 'failed', 'rejected', 'canceled'].includes(state || '');
}

/**
 * Returns true for A2A interrupted states — the task is paused waiting for the
 * human user to respond. Per the A2A spec these are not terminal: the A2A task
 * remains open, but our inflight_task tracking should be closed once the user
 * has been notified, since the reply will arrive as a new inbound email.
 */
function isInterruptedState(state?: TaskState): boolean {
  return ['input-required', 'blocked', 'auth-required'].includes(state || '');
}

function getStateMessage(state?: TaskState): string {
  switch (state) {
    case 'completed':
      return 'Request completed!';
    case 'working':
      return 'Working on your request...';
    case 'submitted':
      return 'Request submitted...';
    case 'blocked':
    case 'input-required':
      return 'I need more information to proceed. Please reply to this email with the requested details.';
    case 'auth-required':
      return 'Authentication is required to proceed. Please reply to this email with the requested credentials.';
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
 * Extract text content from A2A artifacts.
 */
function extractTextFromArtifacts(artifacts?: A2AWebhookPayload['artifacts']): string[] {
  const texts: string[] = [];
  if (!artifacts?.length) return texts;
  for (const artifact of artifacts) {
    for (const part of artifact.parts) {
      if (part.kind === 'text' && part.text) {
        texts.push(part.text);
      }
    }
  }
  return texts;
}

/**
 * Convert webhook artifacts to A2AArtifact[] for the outbound service.
 */
function convertArtifacts(artifacts?: A2AWebhookPayload['artifacts']): A2AArtifact[] | undefined {
  if (!artifacts?.length) return undefined;
  return artifacts.map((a) => ({
    artifactId: a.artifactId,
    name: a.name,
    parts: a.parts,
  }));
}

/**
 * Handle an A2A webhook callback.
 * Called from POST /api/v1/a2a/callback.
 */
export async function handleA2AWebhook(
  payload: A2AWebhookPayload,
  storage: Storage,
  emailOutboundService: EmailOutboundService
): Promise<{ success: boolean; message: string }> {
  const { taskId } = payload;
  logger.info(`Received A2A webhook callback for task ${taskId}, state=${payload.state}`);

  // Look up in-flight task
  const task = await storage.getInFlightTask(taskId);
  if (!task) {
    logger.info(`No in-flight task found for taskId ${taskId} (may have already completed)`);
    return { success: false, message: 'Task not found' };
  }

  // Validate webhook token
  if (task.webhookToken && payload.token !== task.webhookToken) {
    logger.warn(`Invalid webhook token for task ${taskId}`);
    return { success: false, message: 'Invalid webhook token' };
  }

  try {
    // Build the response message
    let message = payload.message || getStateMessage(payload.state);
    const textParts = extractTextFromArtifacts(payload.artifacts);
    if (textParts.length > 0) {
      message = textParts.join('\n\n');
    }
    if (payload.error) {
      message = `${message}\n\nError: ${payload.error}`;
    }

    // Send reply email for final or interrupted states
    if (isFinalState(payload.state) || isInterruptedState(payload.state)) {
      await emailOutboundService.sendReply({
        to: task.senderEmail,
        subject: task.subject || 'A2A Response',
        message,
        artifacts: convertArtifacts(payload.artifacts),
        originalMessageId: task.originalMessageId,
      });
    }

    // Update context if we got a contextId
    if (payload.contextId) {
      await storage.setContext(task.contextKey, payload.contextId, {
        taskId,
        senderEmail: task.senderEmail,
        subject: task.subject,
      });
    }

    // Close in-flight task tracking for final and interrupted states.
    // For interrupted states the A2A task itself remains open, but the user
    // has been notified and their reply will arrive as a new inbound email.
    if (isFinalState(payload.state) || isInterruptedState(payload.state)) {
      await storage.closeInFlightTask(taskId);
      logger.info(
        `Task ${taskId} reached ${isFinalState(payload.state) ? 'terminal' : 'interrupted'} state (${payload.state}), closed tracking`
      );
    }

    return { success: true, message: 'Webhook processed successfully' };
  } catch (error) {
    logger.error(error, `Error processing webhook for task ${taskId}`);

    // Try to notify user of error
    try {
      await emailOutboundService.sendErrorNotification({
        to: task.senderEmail,
        subject: task.subject || 'Error',
        errorMessage: 'An error occurred while processing the response. Please try again.',
        originalMessageId: task.originalMessageId,
      });
    } catch {
      // Ignore notification errors
    }

    return { success: false, message: `Error processing webhook: ${error}` };
  }
}

export { isFinalState, isInterruptedState, getStateMessage };
