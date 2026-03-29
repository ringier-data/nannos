import { Logger } from './logger.js';
import { Storage } from '../storage/storage.js';
import { A2AClientService } from '../services/a2aClientService.js';
import { UserAuthService } from '../services/userAuthService.js';
import { EmailOutboundService } from '../services/emailOutboundService.js';
import { EmailInboundService } from '../services/emailInboundService.js';
import { isFinalState, isInterruptedState, getStateMessage } from './a2aWebhookHandler.js';

const logger = Logger.getLogger('taskRecovery');

/**
 * Recover a single orphaned task by polling A2A for its status.
 */
async function recoverTask(
  task: { taskId: string; senderEmail: string; subject?: string; originalMessageId?: string; contextKey: string },
  storage: Storage,
  a2aClientService: A2AClientService,
  userAuthService: UserAuthService,
  emailOutboundService: EmailOutboundService
): Promise<boolean> {
  const { taskId, senderEmail } = task;
  logger.info(`Recovering orphaned task ${taskId} for ${senderEmail}`);

  try {
    const accessToken = await userAuthService.getOrchestratorToken(senderEmail);
    if (!accessToken) {
      logger.info(`Cannot recover task ${taskId}: user ${senderEmail} not authorized`);
      await storage.closeInFlightTask(taskId);
      return false;
    }

    const response = await a2aClientService.getTaskStatus(taskId, accessToken);

    if (!response.success) {
      logger.info(`Failed to get status for task ${taskId}: ${response.error}`);
      await storage.closeInFlightTask(taskId);

      await emailOutboundService
        .sendErrorNotification({
          to: senderEmail,
          subject: task.subject || 'Request Update',
          errorMessage: 'Sorry, we lost track of your previous request during a restart. Please try again.',
          originalMessageId: task.originalMessageId,
        })
        .catch(() => {});

      return false;
    }

    logger.info(`Task ${taskId} status: ${response.state}`);

    if (isFinalState(response.state)) {
      const message = response.message || getStateMessage(response.state);

      await emailOutboundService.sendReply({
        to: senderEmail,
        subject: task.subject || 'A2A Response',
        message: message,
        artifacts: response.artifacts,
        originalMessageId: task.originalMessageId,
      });

      if (response.contextId) {
        await storage.setContext(task.contextKey, response.contextId, {
          taskId,
          senderEmail,
          subject: task.subject,
        });
      }

      await storage.closeInFlightTask(taskId);
      logger.info(`Successfully recovered task ${taskId}`);
      return true;
    }

    // Interrupted state — task is paused waiting for the user (input-required,
    // auth-required, blocked). Notify the user and close our tracking; their
    // reply will arrive as a new inbound email and re-open the conversation
    // via the preserved context_id in email_context.
    if (isInterruptedState(response.state)) {
      const message = response.message || getStateMessage(response.state);

      await emailOutboundService.sendReply({
        to: senderEmail,
        subject: task.subject || 'A2A Response',
        message: message,
        artifacts: response.artifacts,
        originalMessageId: task.originalMessageId,
      });

      if (response.contextId) {
        await storage.setContext(task.contextKey, response.contextId, {
          taskId,
          senderEmail,
          subject: task.subject,
        });
      }

      await storage.closeInFlightTask(taskId);
      logger.info(`Task ${taskId} is in interrupted state (${response.state}), notified user and closed tracking`);
      return true;
    }

    // Still in progress — leave for webhook
    logger.info(`Task ${taskId} still in progress (${response.state}), leaving for webhook`);
    return false;
  } catch (error) {
    logger.error(error, `Error recovering task ${taskId}`);
    return false;
  }
}

/**
 * Recover orphaned tasks — run on startup and periodically.
 * Scans for in-flight tasks older than minAgeMs and polls A2A for status.
 */
export async function recoverOrphanedTasks(
  storage: Storage,
  a2aClientService: A2AClientService,
  userAuthService: UserAuthService,
  emailOutboundService: EmailOutboundService,
  minAgeMs: number = 2 * 60 * 1000
): Promise<{ recovered: number; failed: number; inProgress: number }> {
  logger.info('Starting orphaned task recovery...');
  const stats = { recovered: 0, failed: 0, inProgress: 0 };

  try {
    const orphanedTasks = await storage.getAllInFlightTasks(minAgeMs);

    if (orphanedTasks.length === 0) {
      logger.info('No orphaned tasks found');
      return stats;
    }

    logger.info(`Found ${orphanedTasks.length} orphaned tasks to recover`);

    for (const task of orphanedTasks) {
      try {
        const recovered = await recoverTask(task, storage, a2aClientService, userAuthService, emailOutboundService);
        if (recovered) {
          stats.recovered++;
        } else {
          const stillExists = await storage.getInFlightTask(task.taskId);
          if (stillExists) {
            stats.inProgress++;
          } else {
            stats.failed++;
          }
        }
      } catch {
        stats.failed++;
      }

      // Small delay to avoid rate limits
      await new Promise((resolve) => setTimeout(resolve, 100));
    }

    logger.info(
      `Task recovery complete: ${stats.recovered} recovered, ${stats.inProgress} in progress, ${stats.failed} failed`
    );
    return stats;
  } catch (error) {
    logger.error(error, 'Task recovery failed');
    return stats;
  }
}

/**
 * Recover pending requests stuck in 'processing' state.
 * These are requests that were claimed (status='processing') after OAuth
 * but the app crashed before processing completed and the row was deleted.
 * Re-runs processInboundEmail for each stuck request.
 */
export async function recoverStuckPendingRequests(
  storage: Storage,
  emailInboundService: EmailInboundService
): Promise<{ recovered: number; failed: number }> {
  logger.info('Checking for stuck pending requests...');
  const stats = { recovered: 0, failed: 0 };

  try {
    const stuckRequests = await storage.getStuckPendingRequests();
    if (stuckRequests.length === 0) {
      logger.info('No stuck pending requests found');
      return stats;
    }

    logger.info(`Found ${stuckRequests.length} stuck pending requests to recover`);

    for (const pending of stuckRequests) {
      try {
        logger.info(`Recovering stuck pending request for ${pending.email}: subject="${pending.subject}"`);

        const inboundEmail = {
          senderEmail: pending.email,
          subject: pending.subject || '',
          bodyText: pending.bodyText || '',
          messageId: pending.originalMessageId || '',
          attachments: [] as any[],
        };

        await emailInboundService.processInboundEmail(inboundEmail);
        await storage.deletePendingRequest(pending.email);
        stats.recovered++;
        logger.info(`Recovered stuck pending request for ${pending.email}`);
      } catch (error) {
        logger.error(error, `Failed to recover stuck pending request for ${pending.email}`);
        stats.failed++;
      }

      await new Promise((resolve) => setTimeout(resolve, 100));
    }

    logger.info(`Pending request recovery complete: ${stats.recovered} recovered, ${stats.failed} failed`);
    return stats;
  } catch (error) {
    logger.error(error, 'Pending request recovery failed');
    return stats;
  }
}

/**
 * Reset processed_email records stuck in 'processing' state.
 * These are emails where the app crashed mid-processing. Resetting them
 * to 'failed' allows a subsequent SNS retry to re-claim and reprocess.
 */
export async function resetStuckProcessedEmails(storage: Storage, minAgeMs: number = 5 * 60 * 1000): Promise<number> {
  try {
    const count = await storage.resetStuckProcessedEmails(minAgeMs);
    if (count > 0) {
      logger.info(`Reset ${count} stuck processed_email records to 'failed'`);
    }
    return count;
  } catch (error) {
    logger.error(error, 'Failed to reset stuck processed emails');
    return 0;
  }
}
