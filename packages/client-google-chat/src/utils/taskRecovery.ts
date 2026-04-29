import { Logger } from './logger.js';
import type { IInFlightTaskStore, InFlightTask, IContextStore } from '../storage/types.js';
import { A2AClientService } from '../services/a2aClientService.js';
import { UserAuthService } from '../services/userAuthService.js';
import { GoogleChatService } from '../services/googleChatService.js';
import { handleTask } from './taskResponseHandler.js';

const logger = Logger.getLogger('taskRecovery');

/**
 * Recover a single orphaned task by polling A2A for its status
 */
async function recoverTask(
  task: InFlightTask,
  chatService: GoogleChatService,
  a2aClientService: A2AClientService,
  userAuthService: UserAuthService,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore
): Promise<boolean> {
  const { taskId, userId, projectId, spaceId, threadId, messageId, statusMessageId, contextKey } = task;

  logger.info(`Recovering orphaned task ${taskId} for user ${userId}`);

  try {
    // Get user's access token for orchestrator audience (token exchange)
    const accessToken = await userAuthService.getOrchestratorToken(userId, projectId);

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
      // Task may have expired on A2A side - clean up
      await inFlightTaskStore.delete(taskId);
      return false;
    }

    if (!response.result.artifacts) response.result.artifacts = [];
    response.result.artifacts?.push({
      artifactId: `final_response_${Date.now()}`,
      parts: response.result.status.message?.parts || [],
    })

    // Build and post response message
    const result = await handleTask({
      task: response.result,
      chatService,
      messageContext: {
        projectId,
        spaceId,
        threadId,
        messageId,
        statusMessageId,
      },
    });

    // Store context ID and last processed timestamp for conversation continuity
    if (result.messageId) {
      await contextStore.set(contextKey, response.result.contextId, messageId);
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
 * Scans store for in-flight tasks and polls A2A for their status
 */
export async function recoverOrphanedTasks(
  inFlightTaskStore: IInFlightTaskStore,
  a2aClientService: A2AClientService,
  userAuthService: UserAuthService,
  chatService: GoogleChatService,
  contextStore: IContextStore,
  minAgeMs: number = 2 * 60 * 1000 // Default: 10 minutes
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
          chatService,
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
