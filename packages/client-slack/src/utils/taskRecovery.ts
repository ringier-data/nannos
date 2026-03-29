import { WebClient } from '@slack/web-api';
import { Logger } from './logger.js';
import type { IInFlightTaskStore, InFlightTask, IContextStore } from '../storage/types.js';
import { A2AClientService } from '../services/a2aClientService.js';
import { UserAuthService } from '../services/userAuthService.js';
import { isTerminatedState, postOrUpdateMessage, formatStatusMessage } from './taskResponseHandler.js';

const logger = Logger.getLogger('taskRecovery');

/**
 * Recover a single orphaned task by polling A2A for its status
 */
async function recoverTask(
  task: InFlightTask,
  slackClient: WebClient,
  a2aClientService: A2AClientService,
  userAuthService: UserAuthService,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore
): Promise<boolean> {
  const { taskId, userId, teamId, channelId, threadTs, messageTs, statusMessageTs, contextKey } = task;

  logger.info(`Recovering orphaned task ${taskId} for user ${userId}`);

  try {
    // Get user's access token for orchestrator audience (token exchange)
    const accessToken = await userAuthService.getOrchestratorToken(userId, teamId);

    if (!accessToken) {
      logger.info(`Cannot recover task ${taskId}: user ${userId} not authorized`);
      // Delete the task - we can't recover without auth
      await inFlightTaskStore.delete(taskId);
      return false;
    }

    // Poll A2A for task status
    const response = await a2aClientService.getTaskStatus(taskId, accessToken);

    if (!response.success) {
      logger.info(`Failed to get status for task ${taskId}: ${response.error}`);
      // Task may have expired on A2A side - clean up
      await inFlightTaskStore.delete(taskId);

      // Notify user that recovery failed
      try {
        await slackClient.chat.postMessage({
          channel: channelId,
          thread_ts: threadTs,
          text: '⚠️ Sorry, I lost track of your previous request during a restart. Please try again.',
        });
      } catch (e) {
        logger.debug(`Failed to notify user about recovery failure: ${e}`);
      }
      return false;
    }

    logger.info(`Task ${taskId} status: ${response.state}`);

    if (isTerminatedState(response.state)) {
      // Task is complete - post result to Slack
      const isSuccess = response.state === 'completed';

      // Build and post response message
      const message = response.message || (isSuccess ? 'Request completed!' : 'Task finished.');
      const fullMessage = formatStatusMessage(response.state, message);

      // Update or post message using centralized handler
      await postOrUpdateMessage(slackClient, channelId, threadTs, fullMessage, statusMessageTs);

      // Store context ID and last processed timestamp for conversation continuity
      if (response.contextId) {
        await contextStore.set(contextKey, response.contextId, messageTs);
      }

      // Clean up - delete the in-flight task record
      await inFlightTaskStore.delete(taskId);

      logger.info(`Successfully recovered task ${taskId}`);
      return true;
    } else {
      // Task is active or interrupted - leave it for webhook
      logger.info(`Task ${taskId} is in state '${response.state}', leaving for webhook`);
      return false;
    }
  } catch (error) {
    logger.error(error, `Error recovering task ${taskId}: ${error}`);
    return false;
  }
}

/**
 * Recover orphaned tasks on startup
 * Scans DynamoDB for in-flight tasks and polls A2A for their status
 */
export async function recoverOrphanedTasks(
  inFlightTaskStore: IInFlightTaskStore,
  a2aClientService: A2AClientService,
  userAuthService: UserAuthService,
  slackClient: WebClient,
  contextStore: IContextStore,
  minAgeMs: number = 2 * 60 * 1000 // Default: 2 minutes
): Promise<{ recovered: number; failed: number; inProgress: number }> {
  logger.info('Starting orphaned task recovery...');

  const stats = { recovered: 0, failed: 0, inProgress: 0 };

  try {
    // Get all orphaned tasks older than minAgeMs
    const orphanedTasks = await inFlightTaskStore.getAll(minAgeMs);

    if (orphanedTasks.length === 0) {
      logger.info('No orphaned tasks found');
      return stats;
    }

    logger.info(`Found ${orphanedTasks.length} orphaned tasks to recover`);

    // Process each task sequentially to avoid rate limits
    for (const task of orphanedTasks) {
      try {
        const result = await recoverTask(
          task,
          slackClient,
          a2aClientService,
          userAuthService,
          contextStore,
          inFlightTaskStore
        );

        if (result) {
          stats.recovered++;
        } else {
          // Check if task was deleted (failed) or left in place (in progress)
          const stillExists = await inFlightTaskStore.get(task.taskId);
          if (stillExists) {
            stats.inProgress++;
          } else {
            stats.failed++;
          }
        }
      } catch (error) {
        logger.error(error, `Failed to recover task ${task.taskId}: ${error}`);
        stats.failed++;
      }

      // Small delay between tasks to avoid rate limits
      await new Promise((resolve) => setTimeout(resolve, 100));
    }

    logger.info(
      `Task recovery complete: ${stats.recovered} recovered, ${stats.inProgress} still in progress, ${stats.failed} failed`
    );

    return stats;
  } catch (error) {
    logger.error(error, `Task recovery failed: ${error}`);
    return stats;
  }
}
