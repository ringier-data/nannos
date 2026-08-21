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
  // Correlation fields echoed by agent-runner so thread replies under the
  // delivered notification can be linked back to the job/run/sub-agent.
  scheduled_job_id?: number;
  scheduled_job_run_id?: number;
  sub_agent_id?: number;
  sub_agent_name?: string;
  prompt?: string;
  error_message?: string;
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
  const { chatService, userAuthStorage, scheduledRunStore } = deps;

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

    const sentMessage = await chatService.sendTextMessage(projectId, dmSpace.name, schedulerPayload.agent_message);

    logger.info(
      `[A2ACallback] Sent notification to user ${userAuth.userId} in space ${dmSpace.name}`
    );

    // Persist the run's provenance keyed by the delivered message's thread, so
    // a thread reply under it can be correlated to the scheduled job/run and
    // forwarded to the orchestrator as a conversation-origin DataPart (see
    // messageHandler).
    const threadName = sentMessage.thread?.name;
    if (threadName && task.contextId) {
      try {
        await scheduledRunStore.set({
          contextKey: scheduledRunStore.buildKey(threadName),
          contextId: task.contextId,
          scheduledJobId: schedulerPayload.scheduled_job_id,
          scheduledJobRunId: schedulerPayload.scheduled_job_run_id,
          subAgentId: schedulerPayload.sub_agent_id,
          subAgentName: schedulerPayload.sub_agent_name,
          prompt: schedulerPayload.prompt,
          resultSummary: schedulerPayload.agent_message,
          schedulerStatus: schedulerPayload.scheduler_status,
          errorMessage: schedulerPayload.error_message,
        });
        logger.info(
          `[A2ACallback] Stored scheduled-run provenance for thread=${threadName} (contextId=${task.contextId})`
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
