/**
 * Handler for A2A push notification callbacks from scheduled agent runs.
 *
 * Flow:
 * 1. Scheduler engine sends a task with pushNotificationConfig (url + secret token)
 * 2. When the task completes/fails, agent-runner POSTs the Task object to this callback
 * 3. The callback route validates X-A2A-Notification-Token against the configured secret
 * 4. We look up the Slack user by their OIDC sub (from task metadata)
 * 5. We send the notification as a DM to the user
 */

import { WebClient } from '@slack/web-api';
import { Task } from '@a2a-js/sdk';
import { Logger } from '../utils/logger.js';
import type { IUserAuthStorage, IScheduledRunStore, BotInstallation } from '../storage/types.js';

const logger = Logger.getLogger('a2aNotificationHandler');

interface SchedulerPayload {
  scheduler_status: string;
  agent_message: string;
  user_sub: string;
  // Correlation fields echoed by agent-runner so thread replies under the
  // delivered notification can be linked back to the job/run/sub-agent.
  scheduled_job_id?: number;
  scheduled_job_run_id?: number;
  sub_agent_id?: number;
  sub_agent_name?: string;
  prompt?: string;
  error_message?: string;
  /**
   * Terminal A2A task state of the sub-agent run ('completed' |
   * 'input_required' | 'failed'), when it reported one. 'input_required'
   * means the run asked the user a question and is waiting for the answer.
   */
  task_state?: string;
}

function getSchedulerPayload(task: Task): SchedulerPayload | undefined {
  if (!task.status.message || task.status.message.parts.length === 0) {
    logger.warn(`[A2ACallback] No task.status.message (taskId=${task.id})`);
    return undefined;
  }

  if (task.status.message.parts[0].kind !== 'text' || !('text' in task.status.message.parts[0])) {
    logger.warn(`[A2ACallback] No task.status.message.parts[0].kind='text' (taskId=${task.id})`);
    return undefined;
  }

  try {
    return JSON.parse(task.status.message.parts[0].text) as SchedulerPayload;
  } catch (e) {
    logger.warn(`[A2ACallback] Error during parsing scheduler payload '${task.status.message.parts[0].text}'`);
  }

  return undefined;
}

export interface A2ANotificationDeps {
  userAuthStorage: IUserAuthStorage;
  scheduledRunStore?: IScheduledRunStore;
  /** Test seam: build the Slack client for a bot token (defaults to `new WebClient(token)`). */
  slackClientFactory?: (botToken: string) => WebClient;
}

/**
 * Handle incoming A2A push notification callback
 */
export async function handleA2ANotification(
  task: Task,
  botInstallation: BotInstallation,
  deps: A2ANotificationDeps,
): Promise<void> {
  const { userAuthStorage, scheduledRunStore, slackClientFactory } = deps;

  const schedulerPayload = getSchedulerPayload(task);
  if (!schedulerPayload) {
    logger.warn(`[A2ACallback] No scheduler payload (taskId=${task.id})`);
    return;
  }

  if (schedulerPayload.scheduler_status === 'condition_not_met') {
    logger.info(`[A2ACallback] Condition is not met (taskId=${task.id})`);
    return;
  }

  // Look up the Slack user by OIDC sub scoped to the authenticated team
  const userAuth = await userAuthStorage.findByOidcSubAndTeam(
    schedulerPayload.user_sub,
    botInstallation.teamId
  );
  if (!userAuth) {
    logger.warn(
      `[A2ACallback] No Slack user found for oidcSub=${schedulerPayload.user_sub} in team=${botInstallation.teamId}`
    );
    return;
  }

  if (!botInstallation.botToken) {
    logger.warn(
      `[A2ACallback] Bot installation ${botInstallation.botName} (team=${botInstallation.teamId}) has no botToken`
    );
    return;
  }

  // Send DM notification to the user via the authenticated team's bot
  try {
    const slackClient = slackClientFactory
      ? slackClientFactory(botInstallation.botToken)
      : new WebClient(botInstallation.botToken);

    const dmResult = await slackClient.conversations.open({ users: userAuth.userId });
    if (!dmResult.ok || !dmResult.channel?.id) {
      logger.warn(
        `[A2ACallback] Could not open DM with user ${userAuth.userId} in team ${botInstallation.teamId}`
      );
      return;
    }

    const postResult = await slackClient.chat.postMessage({
      channel: dmResult.channel.id,
      text: schedulerPayload.agent_message,
    });

    logger.info(
      `[A2ACallback] Sent notification to user ${userAuth.userId} in team ${botInstallation.teamId}`
    );

    // Persist the run's provenance keyed by the delivered message, so a thread
    // reply under it can be correlated to the scheduled job/run and forwarded
    // to the orchestrator as structured data (see messageHandler).
    if (scheduledRunStore && postResult.ts && task.contextId) {
      try {
        await scheduledRunStore.set({
          contextKey: scheduledRunStore.buildKey(dmResult.channel.id, postResult.ts),
          contextId: task.contextId,
          scheduledJobId: schedulerPayload.scheduled_job_id,
          scheduledJobRunId: schedulerPayload.scheduled_job_run_id,
          subAgentId: schedulerPayload.sub_agent_id,
          subAgentName: schedulerPayload.sub_agent_name,
          prompt: schedulerPayload.prompt,
          resultSummary: schedulerPayload.agent_message,
          schedulerStatus: schedulerPayload.scheduler_status,
          errorMessage: schedulerPayload.error_message,
          taskState: schedulerPayload.task_state,
        });
        logger.info(
          `[A2ACallback] Stored scheduled-run provenance for message ts=${postResult.ts} (contextId=${task.contextId})`
        );
      } catch (error) {
        // Provenance is best-effort: the notification itself was delivered.
        logger.error(error, `[A2ACallback] Failed to store scheduled-run provenance: ${error}`);
      }
    }
  } catch (error) {
    logger.error(error, `[A2ACallback] Failed to send DM notification: ${error}`);
  }
}
