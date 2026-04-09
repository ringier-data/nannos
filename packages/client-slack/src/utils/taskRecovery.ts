import { WebClient } from '@slack/web-api';
import { Logger } from './logger.js';
import type { IInFlightTaskStore, InFlightTask, IContextStore } from '../storage/types.js';
import { A2AClientService } from '../services/a2aClientService.js';
import { UserAuthService } from '../services/userAuthService.js';
import { handleTask } from './taskResponseHandler.js';

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

    if ('error' in response) {
      logger.warn({ taskId, error: response.error }, `Failed to get status for task ${taskId}: ${response.error}`);
      await inFlightTaskStore.delete(taskId);
      return false;
    }

    // Build and post response message
    const result = await handleTask({
      task: response.result,
      slackClient,
      messageContext: {
        channelId,
        threadTs,
        messageTs,
        statusMessageTs,
      },
    });

    // Store context ID and last processed timestamp for conversation continuity
    if (result.messageTs) {
      await contextStore.set(contextKey, response.result.contextId, messageTs);
    }

    // Clean up - delete the in-flight task record
    await inFlightTaskStore.delete(taskId);

    logger.info(`Successfully recovered task ${taskId}`);
    return true;
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
  minAgeMs: number = 10 * 60 * 1000 // Default: 10 minutes
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
