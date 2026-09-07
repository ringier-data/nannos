import { WebClient } from '@slack/web-api';
import { Logger } from './logger.js';
import type { IInFlightTaskStore, InFlightTask, IContextStore, IBotInstallationStore } from '../storage/types.js';
import { A2AClientService } from '../services/a2aClientService.js';
import { UserAuthService } from '../services/userAuthService.js';
import { handleTask, postMessage } from './taskResponseHandler.js';

const logger = Logger.getLogger('taskRecovery');

/**
 * How long a task may sit in a non-terminal state before we stop waiting for it
 * and tell the user the turn was lost.
 *
 * A task that never reaches a terminal state is not necessarily still running:
 * if the orchestrator is killed mid-execution (e.g. OOM) nothing re-invokes the
 * run when it restarts, so the stored task stays "submitted"/"working" forever
 * and polling it will never produce an answer. Without a give-up path such a
 * record would simply age out against the store's 1h TTL and the user would
 * never hear anything at all — the silent-failure this whole change exists to
 * remove.
 */
const MAX_RECOVERY_AGE_MS = 30 * 60 * 1000;

/**
 * Recover a single orphaned task by polling A2A for its status
 */
async function recoverTask(
  task: InFlightTask,
  botInstallationStore: IBotInstallationStore,
  fallbackBotToken: string | undefined,
  a2aClientService: A2AClientService,
  userAuthService: UserAuthService,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore
): Promise<boolean> {
  const { taskId, userId, teamId, channelId, threadTs, messageTs, statusMessageTs, contextKey, appId } = task;

  logger.info(`Recovering orphaned task ${taskId} for user ${userId}`);

  try {
    // Resolve the bot token for the app/workspace this task belongs to —
    // tokens are per-installation, so a shared client cannot be used here.
    const bot = appId
      ? await botInstallationStore.getByAppId(appId)
      : (await botInstallationStore.getByTeamId(teamId))[0];
    const botToken = bot?.botToken ?? fallbackBotToken;

    if (!botToken) {
      logger.warn(`Cannot recover task ${taskId}: no bot token found for appId=${appId} teamId=${teamId}`);
      await inFlightTaskStore.delete(taskId);
      return false;
    }

    const slackClient = new WebClient(botToken);

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

    // `handleTask` posts nothing and returns an undefined messageTs when the task
    // has not reached an interrupted/terminal state. Deleting the record in that
    // case throws away the only handle we have on the turn while the user has
    // still seen nothing — the record must survive for a later sweep instead.
    if (!result.messageTs) {
      const ageMs = Date.now() - task.createdAt;

      if (ageMs < MAX_RECOVERY_AGE_MS) {
        logger.info(
          { taskId, state: response.result.status?.state },
          `Task ${taskId} is still non-terminal after ${Math.round(ageMs / 1000)}s; keeping the in-flight record for a later sweep`
        );
        return false;
      }

      // Given up. Tell the user — but only if they are plausibly still waiting.
      //
      // This notice can arrive up to MAX_RECOVERY_AGE_MS after the request. By
      // then the user has often given up on their own, resent, and got a good
      // answer in the same thread. Dropping "please send it again" underneath a
      // conversation that already moved on is worse than saying nothing: it
      // refers to a request they can no longer identify, and invites them to
      // repeat work that already succeeded.
      //
      // `lastProcessedTs` advances on every delivered turn for this thread, and
      // for THIS turn it was pinned to our own messageTs when the task started.
      // So a strictly-later value means another turn in this thread completed
      // after ours was submitted — the user moved past the failure.
      const context = await contextStore.get(contextKey).catch(() => null);
      const lastProcessedTs = context?.lastProcessedTs;
      const supersededByLaterTurn =
        !!lastProcessedTs && parseFloat(lastProcessedTs) > parseFloat(messageTs);

      if (supersededByLaterTurn) {
        logger.info(
          { taskId, state: response.result.status?.state, lastProcessedTs, messageTs },
          `Task ${taskId} never reached a terminal state after ${Math.round(ageMs / 60000)}min, but a later turn in this thread has since completed — dropping the record without notifying the user`
        );
      } else {
        logger.warn(
          { taskId, state: response.result.status?.state },
          `Task ${taskId} never reached a terminal state after ${Math.round(ageMs / 60000)}min; giving up and notifying the user`
        );
        await postMessage(
          slackClient,
          channelId,
          threadTs,
          "⚠️ I couldn't finish an earlier request in this thread — the agent was interrupted while working on it and I can't recover the answer. Please send it again if you still need it."
        ).catch((err) => logger.error(err, `Failed to post give-up notice for task ${taskId}: ${err}`));
      }

      await inFlightTaskStore.delete(taskId);
      return false;
    }

    // Store context ID and last processed timestamp for conversation continuity
    await contextStore.set(contextKey, response.result.contextId, messageTs);

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
  botInstallationStore: IBotInstallationStore,
  contextStore: IContextStore,
  fallbackBotToken?: string,
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
          botInstallationStore,
          fallbackBotToken,
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
