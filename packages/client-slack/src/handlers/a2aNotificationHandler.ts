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
import type { IUserAuthStorage, BotInstallation } from '../storage/types.js';

const logger = Logger.getLogger('a2aNotificationHandler');

interface SchedulerPayload {
  scheduler_status: string;
  agent_message: string;
  user_sub: string;
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
}

/**
 * Handle incoming A2A push notification callback
 */
export async function handleA2ANotification(
  task: Task,
  botInstallation: BotInstallation,
  deps: A2ANotificationDeps,
): Promise<void> {
  const { userAuthStorage } = deps;

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
    const slackClient = new WebClient(botInstallation.botToken);

    const dmResult = await slackClient.conversations.open({ users: userAuth.userId });
    if (!dmResult.ok || !dmResult.channel?.id) {
      logger.warn(
        `[A2ACallback] Could not open DM with user ${userAuth.userId} in team ${botInstallation.teamId}`
      );
      return;
    }

    await slackClient.chat.postMessage({
      channel: dmResult.channel.id,
      text: schedulerPayload.agent_message,
    });

    logger.info(
      `[A2ACallback] Sent notification to user ${userAuth.userId} in team ${botInstallation.teamId}`
    );
  } catch (error) {
    logger.error(error, `[A2ACallback] Failed to send DM notification: ${error}`);
  }
}
