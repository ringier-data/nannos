import _ from 'lodash';
import { randomUUID } from 'crypto';

import { Logger } from '../utils/logger.js';
import {
  GoogleChatAttachment,
  processAttachmentsToS3,
  getFileProcessingWarnings,
} from '../utils/fileUtils.js';
import { UserAuthService } from '../services/userAuthService.js';
import { A2AGoogleChatBasedRequest } from '../services/a2aClientService.js';
import { GoogleChatService } from '../services/googleChatService.js';
import type { Message, Task, TaskStatusUpdateEvent } from '@a2a-js/sdk';
import type { ContextRecord } from '../storage/types.js';
import { handleTask, handleError } from '../utils/taskResponseHandler.js';
import { HandlerDependencies } from './types.js';
import { getSpinnerVerb } from '../utils/spinnerVerbs.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type MessageSource = 'space_message' | 'direct_message';

/**
 * Normalized message shape for Google Chat events.
 */
export interface NormalizedMessage {
  userId: string;
  userEmail: string;
  projectId: string;
  spaceId: string;
  messageId: string;
  threadId: string;
  rawText: string;
  attachments?: GoogleChatAttachment[];
  source: MessageSource;
}

// ---------------------------------------------------------------------------
// Thread history via Google Chat API
// ---------------------------------------------------------------------------

interface ThreadHistoryResult {
  historyXml?: string;
  attachments?: GoogleChatAttachment[];
}

/**
 * Fetch thread history using the Google Chat spaces.messages.list API.
 * @see https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/list
 */
async function fetchThreadHistory(
  chatService: GoogleChatService,
  projectId: string,
  spaceId: string,
  threadId: string,
  currentMessageId: string,
  statusMessageId?: string,
  sinceMessageId?: string
): Promise<ThreadHistoryResult> {
  const logger = Logger.getLogger('fetchThreadHistory');
  logger.info(
    `Fetching thread history for space ${spaceId}, thread ${threadId} since ${sinceMessageId || 'beginning'}`
  );
  const result: ThreadHistoryResult = {};

  try {
    const messages = await chatService.listMessages(projectId, spaceId, threadId, 100);

    if (!messages || messages.length === 0) {
      return result;
    }

    const formatRelativeTime = (timestamp: Date): string => {
      const now = new Date();
      const diffMs = now.getTime() - timestamp.getTime();
      const diffSecs = Math.floor(diffMs / 1000);
      const diffMins = Math.floor(diffSecs / 60);
      const diffHours = Math.floor(diffMins / 60);
      const diffDays = Math.floor(diffHours / 24);

      if (diffSecs < 60) return `${diffSecs} seconds ago`;
      if (diffMins < 60) return `${diffMins} minute${diffMins === 1 ? '' : 's'} ago`;
      if (diffHours < 24) return `${diffHours} hour${diffHours === 1 ? '' : 's'} ago`;
      return `${diffDays} day${diffDays === 1 ? '' : 's'} ago`;
    };

    // If sinceMessageId is provided, skip messages up to and including it
    let startIndex = 0;
    if (sinceMessageId) {
      const sinceIdx = messages.findIndex((m) => m.name === sinceMessageId);
      if (sinceIdx >= 0) {
        startIndex = sinceIdx;
      }
    }

    const filteredMessages = messages.slice(startIndex)
      .filter((msg) => msg.name !== currentMessageId && msg.name !== statusMessageId);

    const historyMessages = filteredMessages
      .map((msg) => {
        const msgTimestamp = msg.createTime ? new Date(msg.createTime) : new Date();
        const isoTimestamp = msgTimestamp.toISOString();
        const relativeTime = formatRelativeTime(msgTimestamp);

        const role = msg.sender?.type === 'BOT' ? 'assistant' : 'user';
        const userId = msg.sender?.name || '';
        const userName = msg.sender?.displayName || '';
        return `<message role="${role}" userId="${userId}" userName="${userName}" timestamp="${isoTimestamp}" relativeTime="${relativeTime}">${msg.text || ''}</message>`;
      })
      .filter(Boolean);

    if (historyMessages.length > 0) {
      result.historyXml = `<thread_context>\n${historyMessages.join('\n')}\n</thread_context>`;
    }

    // Collect attachments from all history messages
    const attachments: GoogleChatAttachment[] = filteredMessages.flatMap((msg) =>
      (msg.attachment ?? [])
        .filter((a) => a.source === 'UPLOADED_CONTENT' && a.attachmentDataRef?.resourceName)
        .map((a) => ({
          name: a.name ?? '',
          contentName: a.contentName ?? '',
          contentType: a.contentType ?? 'application/octet-stream',
          attachmentDataRef: a.attachmentDataRef
            ? { resourceName: a.attachmentDataRef.resourceName ?? '' }
            : undefined,
          source: 'UPLOADED_CONTENT',
        }))
    );

    if (attachments.length > 0) {
      result.attachments = attachments;
    }

    logger.info(`Fetched ${historyMessages.length} messages and ${attachments.length} attachments from thread history`);
    return result;
  } catch (error) {
    logger.debug(`Failed to fetch thread history: ${error}`);
    return result;
  }
}

// ---------------------------------------------------------------------------
// Authorization prompt
// ---------------------------------------------------------------------------

/**
 * Send an authorization-required prompt with a card button.
 * Google Chat doesn't have ephemeral messages, so this sends a regular card.
 */
async function sendAuthorizationRequired(
  chatService: GoogleChatService,
  spaceId: string,
  userId: string,
  projectId: string,
  threadId: string,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('sendAuthorizationRequired');
  logger.info(`Sending authorization required message to user ${userId} in space ${spaceId}, thread ${threadId}`);

  try {
    const state = `gchat-auth-${Date.now()}-${userId}`;
    await userAuthService.storeAuthState(state, userId, projectId);

    const config = await import('../config/config.js').then((m) => m.getConfigFromEnv());
    const appAuthorizeUrl = new URL(`/api/v1/authorize?state=${encodeURIComponent(state)}`, config.baseUrl).toString();

    const card = chatService.buildAuthCard(
      appAuthorizeUrl,
      '🔐 Authorization Required',
      'You need to authorize this bot to use A2A services on your behalf.',
      'Authorize Now'
    );

    await chatService.sendPrivateCardMessage(projectId, spaceId, userId, [card], threadId);

    logger.info(`Successfully sent authorization card message`);
  } catch (error) {
    logger.error(error, `Failed to send authorization required message to user ${userId}: ${error}`);
    throw error;
  }
}

// ---------------------------------------------------------------------------
// Unified message handler
// ---------------------------------------------------------------------------

/**
 * Unified handler for Google Chat MESSAGE events.
 *
 * This is the single entry-point for processing user messages regardless of
 * whether they come from a space @mention or a DM.
 */
export async function handleIncomingMessage(msg: NormalizedMessage, deps: HandlerDependencies): Promise<void> {
  const logger = Logger.getLogger('handleIncomingMessage');
  const {
    userId,
    userEmail,
    projectId,
    spaceId,
    messageId,
    threadId,
    rawText,
    attachments: eventAttachments,
    source,
  } = msg;
  const {
    userAuthService,
    a2aClientService,
    chatService,
    contextStore,
    pendingRequestStore,
    inFlightTaskStore,
    fileStorageService,
    baseUrl,
    isLocalMode,
    feedbackService,
  } = deps;

  let statusMessageId: string | undefined;
  try {
    logger.info(`${source} from user ${userId} in space ${spaceId}`);

    const cleanText = rawText.trim();

    if (!cleanText && (!eventAttachments || eventAttachments.length === 0)) {
      return;
    }

    // ---- Authorization check ----
    let isAuthorized = false;
    try {
      isAuthorized = await userAuthService.isUserAuthorized(userId, projectId);
    } catch (error: any) {
      if (error.message?.includes('does not exist')) {
        logger.error(error, `Storage configuration error: ${error.message}`);
        await chatService.sendPrivateTextMessage(
          projectId,
          spaceId,
          userId,
          '⚠️ The system is not properly configured. Please contact your administrator.',
          threadId
        );
        return;
      }
      throw error;
    }

    if (!isAuthorized) {
      logger.info(`User ${userId} is not authorized, will prompt for authorization`);
      await pendingRequestStore.set({
        visitorId: pendingRequestStore.buildVisitorId(projectId, userId),
        text: cleanText,
        spaceId,
        threadId,
        messageId,
        userEmail,
        source,
        createdAt: Date.now(),
      });
      await sendAuthorizationRequired(chatService, spaceId, userId, projectId, threadId, userAuthService);
      return;
    }

    // ---- Get orchestrator access token ----
    const accessToken = await userAuthService.getOrchestratorToken(userId, projectId);

    if (!accessToken) {
      logger.error(`Failed to get access token for user ${userId}`);
      await chatService.sendPrivateTextMessage(
        projectId,
        spaceId,
        userId,
        '❌ Your authorization has expired. Please authorize again.',
        threadId
      );
      await pendingRequestStore.set({
        visitorId: pendingRequestStore.buildVisitorId(projectId, userId),
        text: cleanText,
        spaceId,
        threadId,
        messageId,
        userEmail,
        source,
        createdAt: Date.now(),
      });
      await sendAuthorizationRequired(chatService, spaceId, userId, projectId, threadId, userAuthService);
      return;
    }

    // Post an immediate "Working..." message so the user sees responsiveness
    // before the A2A server sends its first status-update event.
    const statusMessage = {
      thinking: `🧠 ${getSpinnerVerb() || 'Working'}...`,
      activity: '',
      todos: '',
    };

    try {
      const immediateStatus = await chatService.sendMessage({
        projectId,
        spaceId,
        threadId,
        text: statusMessage.thinking,
        messageReplyOption: 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD',
      });
      statusMessageId = immediateStatus.name || undefined;
    } catch (err) {
      logger.debug(`Failed to post immediate status message: ${err}`);
    }

    // ---- Context ----
    const contextKey = contextStore.buildKey(projectId, spaceId, threadId);
    const existingContext: ContextRecord | null = await contextStore.get(contextKey);
    const existingContextId = existingContext?.contextId;

    // ---- Build XML-wrapped request text ----
    const isoTimestamp = new Date().toISOString();

    let requestText = cleanText;
    const isInThread = threadId !== messageId; // If thread ID differs from message ID, we're in a thread
    let threadAttachments: GoogleChatAttachment[] = [];

    const buildAttachedFilesXml = (attachments: GoogleChatAttachment[]): string => {
      if (attachments.length === 0) return '';
      const fileElements = attachments.map((a) => `<file name="${a.contentName}" type="${a.contentType}" />`).join('');
      return `\n  <attachedFiles>${fileElements}</attachedFiles>`;
    };

    const currentFilesXml = eventAttachments ? buildAttachedFilesXml(eventAttachments) : '';

    if (isInThread) {
      const sinceMessageId = existingContext?.lastProcessedMessageId;
      const threadHistoryResult = await fetchThreadHistory(
        chatService,
        projectId,
        spaceId,
        threadId,
        messageId,
        statusMessageId,
        sinceMessageId
      );

      threadAttachments = threadHistoryResult.attachments || [];

      if (threadHistoryResult.historyXml) {
        requestText = `${threadHistoryResult.historyXml}\n<current_request userId="${userId}" timestamp="${isoTimestamp}">${cleanText}${currentFilesXml}</current_request>`;
        logger.info(`Included thread history ${sinceMessageId ? 'since last interaction' : '(full)'}`);
      } else {
        requestText = `<message role="user" userId="${userId}" timestamp="${isoTimestamp}">${cleanText}${currentFilesXml}</message>`;
      }
    } else {
      requestText = `<message role="user" userId="${userId}" timestamp="${isoTimestamp}">${cleanText}${currentFilesXml}</message>`;
    }

    // ---- Process files ----
    const webhookUrl = new URL(`/api/v1/a2a/callback`, baseUrl).toString();
    const webhookToken = randomUUID();

    const seenAttachmentsNames = new Set<string>();
    const allAttachments: GoogleChatAttachment[] = [];

    if (eventAttachments && eventAttachments.length > 0) {
      for (const attachment of eventAttachments) {
        if (!seenAttachmentsNames.has(attachment.name)) {
          seenAttachmentsNames.add(attachment.name);
          allAttachments.push(attachment);
        }
      }
    }

    for (const attachment of threadAttachments) {
      if (!seenAttachmentsNames.has(attachment.name)) {
        seenAttachmentsNames.add(attachment.name);
        allAttachments.push(attachment);
      }
    }

    let processedFiles: Awaited<ReturnType<typeof processAttachmentsToS3>> = [];

    if (allAttachments && allAttachments.length > 0) {
      logger.info(`Processing ${allAttachments.length} attachment(s)`);

      const warnings = getFileProcessingWarnings(allAttachments);
      if (warnings.length > 0) {
        await chatService.sendPrivateTextMessage(
          projectId,
          spaceId,
          userId,
          `⚠️ Some files could not be processed:\n${warnings.join('\n')}`,
          threadId
        );
      }

      processedFiles = await processAttachmentsToS3(
        projectId,
        allAttachments,
        chatService,
        fileStorageService,
        userId,
        userEmail,
        contextKey
      );
      logger.info(`Successfully processed ${processedFiles.length} of ${allAttachments.length} attachment(s) to S3`);
    }

    // ---- Build & send A2A request via streaming ----
    const a2aRequest: A2AGoogleChatBasedRequest = {
      userId,
      projectId,
      spaceId,
      threadId: isInThread ? threadId : undefined,
      messageId,
      text: requestText,
      fileUrls:
        processedFiles.length > 0
          ? processedFiles.map((f) => ({
              name: f.name,
              mimeType: f.mimeType,
              url: f.url,
            }))
          : undefined,
      contextId: existingContextId || undefined,
      webhookUrl: isLocalMode ? undefined : webhookUrl,
      webhookToken: isLocalMode ? undefined : webhookToken,
    };

    logger.info('Sending message via streaming');

    let accumulatedTask: Task | null = null;
    let feedbackRequestData: { sub_agents?: string[] } | null = null;
    try {
      for await (const event of a2aClientService.sendMessageStream(a2aRequest, accessToken)) {
        logger.debug(`Stream event: ${_.get(event, 'kind')}`);
        logger.trace(event, `Stream event details:`);

        if (event.kind === 'task') {
          // according to spec this is the first message...
          const task = event as Task;
          accumulatedTask = task;

          await inFlightTaskStore.save({
            taskId: accumulatedTask.id,
            visitorId: inFlightTaskStore.buildVisitorId(projectId, userId),
            userId,
            projectId,
            spaceId,
            threadId,
            messageId,
            statusMessageId,
            contextKey,
            webhookToken,
            source,
            createdAt: Date.now(),
          });

          // Store context ID if we got one
          await contextStore.set(contextKey, accumulatedTask.contextId ?? '', messageId);
        } else if (event.kind === 'message') {
          const message = event as Message;
          logger.debug({ taskId: message.taskId, message }, `Stream message received. Doing nothing.`);
        } else if (!accumulatedTask) {
          logger.debug(`Received ${_.get(event, 'kind')} before task. Bug in this app or A2A server? Ignoring.`);
        } else if (event.kind === 'status-update') {
          const statusEvent = event as TaskStatusUpdateEvent;

          // Update final response state
          accumulatedTask.status = statusEvent.status;

          if (event.status.message?.extensions?.includes('urn:nannos:a2a:work-plan:1.0')) {
            const workPlan = event.status.message.parts.find((x) => x.kind === 'data')?.data as {
              todos: Array<{
                name: string; // Required: task description
                state: 'submitted' | 'working' | 'completed' | 'failed'; // Required: current state
                source?: string; // Optional: agent that owns this todo
                target?: string; // Optional: resource ID being operated on
              }>;
            };
            statusMessage.todos = workPlan.todos
              .map(
                (x) =>
                  `• ${x.state === 'completed' ? '✅' : x.state === 'working' ? '⏳' : x.state === 'failed' ? '❌' : '🔜'} ${x.name}${x.source ? ` (agent ${x.source})` : ''}${x.target ? ` [${x.target}]` : ''}`
              )
              .join('\n');
          } else if (event.status.message?.extensions?.includes('urn:nannos:a2a:activity-log:1.0')) {
            statusMessage.activity = event.status.message.parts.find((x) => x.kind === 'text')?.text || '';
          } else if (event.status.message?.extensions?.includes('urn:nannos:a2a:feedback-request:1.0')) {
            const feedbackData = event.status.message.parts.find((x) => x.kind === 'data')?.data as {
              sub_agents?: string[];
            };
            logger.debug({ feedbackData }, `Received feedback-request extension`);
            feedbackRequestData = feedbackData;
          } else if (event.status?.state === 'completed') {
            if (!accumulatedTask.artifacts) accumulatedTask.artifacts = [];
            accumulatedTask.artifacts?.push({
              artifactId: `final_response_${Date.now()}`,
              parts: event.status.message?.parts || [],
            })
          } else {
            logger.debug(`Received status update without recognized extensions. Not updating status message details.`);
          }
          const newStatusMessage = `${statusMessage.thinking}${statusMessage.activity ? ` [${statusMessage.activity}]` : ''}${statusMessage.todos ? `\n${statusMessage.todos}` : ''}`;
          if (statusMessageId) {
            await chatService.updateMessage({
              projectId,
              messageName: statusMessageId,
              text: newStatusMessage,
            })
          }
        } else if (event.kind === 'artifact-update') {
          if (!accumulatedTask.artifacts) accumulatedTask.artifacts = [];
          accumulatedTask.artifacts.push(event.artifact);
        } else {
          logger.debug({ taskId: accumulatedTask?.id }, `Unknown stream event: ${_.get(event, 'kind')}`);
        }

        if (accumulatedTask) {
          await inFlightTaskStore.touch(accumulatedTask.id).catch((err) => {
            logger.error(err, `Failed to update in-flight task timestamp for task ${accumulatedTask?.id}: ${err}`);
          });
        }
        logger.debug({ taskId: accumulatedTask?.id }, `Current state: ${accumulatedTask?.status?.state}`);
      }
    } catch (error) {
      logger.error(error, `A2A stream error: ${error}`);
    }

    // Build the final response
    if (!accumulatedTask) {
      logger.error(
        `No task information received from A2A server. Silently failing without sending a response to the user.`
      );
      return;
    }

    // ---- Handle the response ----
    const result = await handleTask({
      task: accumulatedTask,
      chatService,
      messageContext: {
        projectId,
        spaceId,
        threadId,
        messageId,
        statusMessageId,
      },
      includeFeedbackButtons: !!feedbackService,
    });

    await inFlightTaskStore.delete(accumulatedTask.id);

    if (result.messageId) {
      contextStore.set(contextKey, accumulatedTask?.contextId, result.messageId).catch((err) => {
        logger.error(err, `Failed to update context store for task ${accumulatedTask?.id}: ${err}`);
      });

      // Store response mapping so button clicks can be correlated to A2A IDs
      if (feedbackService && accumulatedTask.contextId) {
        feedbackService.responseMapping.set(result.messageId, {
          contextId: accumulatedTask.contextId,
          taskId: accumulatedTask.id,
          userId,
          projectId,
          subAgents: feedbackRequestData?.sub_agents,
          createdAt: Date.now(),
        });
      }
    }
  } catch (error) {
    logger.error(error, `Error handling ${source}: ${error}`);
    await handleError(chatService, projectId, spaceId, threadId);
  }
}
