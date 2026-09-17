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
import { authPromptFromPayload } from '../utils/inTaskAuth.js';
import type { ReplyTo } from '../services/scheduledRunResumeService.js';
import { HandlerDependencies } from './types.js';
import { Task } from '@a2a-js/sdk';

const logger = Logger.getLogger('a2aNotificationHandler');

interface SchedulerPayload {
  scheduler_status: string;
  /**
   * The in-task-auth ask, when the run PARKED on its owner's credential. Carried
   * inside this payload rather than as the status message itself: part zero is parsed
   * as this JSON, so a status message shaped only by the auth extension would fail
   * `JSON.parse` and the notification would be dropped entirely. See ADR-0009.
   */
  auth_payload?: Record<string, unknown>;
  /** Where the answer goes. Logical, never a URL — see ScheduledRunResumeService. */
  reply_to?: ReplyTo;
  /**
   * Where the ask this run is continuing was posted, echoed back untouched by
   * console-backend. Only this client can read it: it is the space and thread of a
   * message this client sent, and it travelled with the click that answered the ask.
   */
  reply_to_message?: { space?: string; thread?: string };
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

  const parked = schedulerPayload.scheduler_status === 'auth_required';

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

    // A parked run asks rather than reports, using the card this client already
    // renders for an interactive authorization. Only where the answer goes differs,
    // which is what `replyTo` carries into the button parameters.
    const authPrompt = parked ? authPromptFromPayload(schedulerPayload.auth_payload) : null;
    // Whatever a resumed run produces belongs under the ask that unblocked it: the owner
    // sees one exchange — "I need permission", "here is what I did" — instead of loose
    // notices they have to connect themselves. That includes a SECOND ask, when one
    // authorization leads straight to another: the chain stays legible as a chain
    // (ADR-0009 calls it load-bearing).
    //
    // Only when the ask was posted in the space this notification is going to, since it
    // is this client's own message it threads under. The coordinates came back untouched
    // from console-backend, which stores them opaquely.
    const askThreadId =
      schedulerPayload.reply_to_message?.space === dmSpace.name
        ? schedulerPayload.reply_to_message?.thread
        : undefined;
    const sentMessage = authPrompt
      ? await chatService.sendMessage({
          projectId,
          spaceId: dmSpace.name,
          // The prose stays alongside the card: it carries the authorize URL for any
          // surface that shows text without rendering cards.
          text: schedulerPayload.agent_message,
          cardsV2: [
            chatService.buildInTaskAuthCard(deps.config, authPrompt, {
              taskId: task.id,
              replyTo: schedulerPayload.reply_to,
            }),
          ],
          ...(askThreadId
            ? { threadId: askThreadId, messageReplyOption: 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD' as const }
            : {}),
        })
      : await chatService.sendTextMessage(
          projectId,
          dmSpace.name,
          schedulerPayload.agent_message,
          askThreadId
        );

    logger.info(
      `[A2ACallback] Sent notification to user ${userAuth.userId} in space ${dmSpace.name}`
    );

    // Persist the run's provenance keyed by the delivered message's thread, so
    // a thread reply under it can be correlated to the scheduled job/run and
    // forwarded to the orchestrator as a conversation-origin DataPart (see
    // messageHandler).
    // NOT for the ask: ADR-0008 keys an adopted sub-agent's memory by the RUN only
    // because "a run is adoptable exactly once", and recording the ask too would give
    // one run two adoptable threads onto one sub-agent conversation. Nothing is lost —
    // a prose reply cannot resume a parked run anyway.
    const threadName = sentMessage.thread?.name;
    if (threadName && task.contextId && !parked) {
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
          taskState: schedulerPayload.task_state,
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
