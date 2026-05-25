/**
 * Handler for A2A push notification callbacks from scheduled agent runs.
 *
 * Flow:
 * 1. Scheduler engine sends a task with pushNotificationConfig (url + secret token)
 * 2. When the task completes/fails, agent-runner POSTs the Task object to this callback
 * 3. a2aNotificationAuth middleware validates X-A2A-Notification-Token and resolves projectId
 * 4. We look up the Google Chat user by their OIDC sub (from task metadata)
 * 5. We send the notification as a DM to the user
 */

import { Logger } from '../utils/logger.js';
import { HandlerDependencies } from './types.js';
import { Task } from '@a2a-js/sdk';

const logger = Logger.getLogger('a2aNotificationHandler');

interface SchedulerPayload {
  scheduler_status: string;
  agent_message: string;
  user_sub: string;
}

function getSchedulerPayload(task: Task)  {
  if (!task.status.message || task.status.message.parts.length === 0) {
    logger.warn(`[A2ACallback] No task.status.message (taskId=${task.id})`);
    return undefined;
  }

  if (task.status.message.parts[0].kind !== 'text' || !('text' in task.status.message.parts[0])) {
    logger.warn(`[A2ACallback] No task.status.message.parts[0].kind='text' (taskId=${task.id})`);
    return undefined;
  }

  try {
    return JSON.parse(task.status.message.parts[0].text) as SchedulerPayload
  } catch (e) {
    logger.warn(`[A2ACallback] Error during parsing scheduler payload '${task.status.message.parts[0].text}'`)
  }

  return undefined;
}

/**
 * Handle incoming A2A push notification callback
 */
export async function handleA2ANotification(
  task: Task,
  projectId: string,
  deps: HandlerDependencies,
): Promise<void> {
  const { chatService, userAuthStorage } = deps;

  const schedulerPayload = getSchedulerPayload(task)
  if (!schedulerPayload) {
    logger.warn(`[A2ACallback] No scheduler payload (taskId=${task.id})`);
    return;
  }

  if (schedulerPayload.scheduler_status === 'condition_not_met') {
    logger.warn(`[A2ACallback] Condition is not met  (taskId=${task.id})`);
    return;
  }

  // Look up the Google Chat user by their OIDC sub for this project
  const userAuth = await userAuthStorage.findByOidcSub(schedulerPayload.user_sub, projectId);
  if (!userAuth) {
    logger.warn(
      `[A2ACallback] No Google Chat user found for oidcSub=${schedulerPayload.user_sub} in project=${projectId}`
    );
    return;
  }

  // Find the user's DM space and send the notification
  try {
    const dmSpace = await chatService.findDirectMessage(projectId, userAuth.userId);
    if (!dmSpace?.name) {
      logger.warn(
        `[A2ACallback] No DM space found for user ${userAuth.userId} in project ${projectId}`
      );
      return;
    }

    await chatService.sendTextMessage(projectId, dmSpace.name, schedulerPayload.agent_message);

    logger.info(
      `[A2ACallback] Sent notification to user ${userAuth.userId} in space ${dmSpace.name}`
    );
  } catch (error) {
    logger.error(error, `[A2ACallback] Failed to send DM notification: ${error}`);
  }
}
