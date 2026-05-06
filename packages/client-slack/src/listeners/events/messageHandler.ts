import { WebClient } from '@slack/web-api';
import { randomUUID } from 'crypto';
import { Logger } from '../../utils/logger.js';
import {
  SlackFile,
  processSlackFilesToS3,
  getFileProcessingWarnings,
  hasProcessableFiles,
} from '../../utils/fileUtils.js';
import { UserAuthService } from '../../services/userAuthService.js';
import { A2AClientService, A2ASlackBasedRequest } from '../../services/a2aClientService.js';
import type { Message, Task, TaskStatusUpdateEvent } from '@a2a-js/sdk';
import { FileStorageService } from '../../services/fileStorageService.js';
import type { IContextStore, IPendingRequestStore, IInFlightTaskStore, ContextRecord } from '../../storage/types.js';
import { handleTask, handleError, postMessage } from '../../utils/taskResponseHandler.js';
import { FeedbackService } from '../../services/feedbackService.js';
import _ from 'lodash';
import { getSpinnerVerb } from '../../utils/spinnerVerbs.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type MessageSource = 'app_mention' | 'direct_message';

/**
 * Normalized message shape that both app_mention and DM events are mapped into.
 */
export interface NormalizedMessage {
  userId: string;
  teamId: string;
  channelId: string;
  messageTs: string;
  threadTs: string;
  rawText: string;
  files?: SlackFile[];
  dataParts?: Record<string, unknown>[]; // Structured data (e.g., HITL decisions)
  source: MessageSource;
  appId?: string; // Slack App ID (api_app_id from body) for multi-bot token routing
  client: WebClient;
}

/**
 * All shared dependencies the handler needs.
 */
export interface HandlerDependencies {
  userAuthService: UserAuthService;
  a2aClientService: A2AClientService;
  contextStore: IContextStore;
  pendingRequestStore: IPendingRequestStore;
  inFlightTaskStore: IInFlightTaskStore;
  baseUrl: string;
  botToken: string;
  botName: string; // Personalized bot display name, resolved from botInstallation per event
  fileStorageService: FileStorageService;
  isLocalMode: boolean;
  feedbackService?: FeedbackService;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Resolve `<@USERID>` mentions in text to `@DisplayName`.
 */
async function resolveMentions(text: string, client: WebClient): Promise<string> {
  const logger = Logger.getLogger('resolveMentions');
  const mentionPattern = /<@([A-Z0-9]+)>/g;
  const matches = [...text.matchAll(mentionPattern)];

  if (matches.length === 0) return text;

  const uniqueIds = [...new Set(matches.map((m) => m[1]))];
  const nameMap = new Map<string, string>();

  await Promise.all(
    uniqueIds.map(async (uid) => {
      try {
        const info = await client.users.info({ user: uid });
        if (info.user) {
          nameMap.set(uid, info.user.profile?.display_name || info.user.real_name || info.user.name || uid);
        }
      } catch (e) {
        logger.debug(`Failed to resolve user ${uid}: ${e}`);
      }
    })
  );

  return text.replace(mentionPattern, (_, uid) => {
    const name = nameMap.get(uid);
    return name ? `@${name}` : '';
  });
}

function formatTimestamp(ts: number): string {
  return new Date(ts).toISOString();
}

function formatSlackTs(slackTs: string): string {
  const epochSeconds = parseFloat(slackTs);
  return new Date(epochSeconds * 1000).toISOString();
}

// ---------------------------------------------------------------------------
// Debug commands
// ---------------------------------------------------------------------------

/**
 * Handle "debug" command – shows thread context, in-flight tasks, auth status.
 */
async function handleDebugCommand(
  client: WebClient,
  channelId: string,
  userId: string,
  teamId: string,
  threadTs: string,
  _messageTs: string,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore,
  userAuthService: UserAuthService,
  botName: string
): Promise<void> {
  const logger = Logger.getLogger('handleDebugCommand');
  logger.info(`debug command from user ${userId} in thread ${threadTs}`);

  const debugInfo: string[] = [];
  debugInfo.push(`🔍 *${botName} Debug Information*\n`);

  debugInfo.push('*Identifiers:*');
  debugInfo.push(`• Team ID: \`${teamId}\``);
  debugInfo.push(`• Channel ID: \`${channelId}\``);
  debugInfo.push(`• User ID: \`${userId}\``);
  debugInfo.push(`• Thread TS: \`${threadTs}\` (${formatSlackTs(threadTs)})`);
  debugInfo.push('');

  const isAuthorized = await userAuthService.isUserAuthorized(userId, teamId);
  debugInfo.push('*Login Status:*');
  debugInfo.push(`• Status: ${isAuthorized ? '✅ Logged in' : '❌ Not logged in'}`);
  debugInfo.push('');

  const contextKey = contextStore.buildKey(teamId, channelId, threadTs);
  const contextRecord = await contextStore.get(contextKey);

  debugInfo.push('*Thread Context:*');
  debugInfo.push(`• Context Key: \`${contextKey}\``);
  if (contextRecord) {
    debugInfo.push(`• Context ID: \`${contextRecord.contextId}\``);
    if (contextRecord.lastProcessedTs) {
      debugInfo.push(`• Last Processed: ${formatSlackTs(contextRecord.lastProcessedTs)}`);
    }
  } else {
    debugInfo.push('• No context found for this thread');
  }
  debugInfo.push('');

  const inFlightTasks = await inFlightTaskStore.getByUser(teamId, userId);
  const threadTasks = inFlightTasks.filter((t) => t.threadTs === threadTs);

  debugInfo.push('*In-Flight Tasks (this thread):*');
  if (threadTasks.length === 0) {
    debugInfo.push('• No in-flight tasks in this thread');
  } else {
    for (const task of threadTasks) {
      debugInfo.push(`• Task ID: \`${task.taskId}\``);
      debugInfo.push(`  - Source: ${task.source}`);
      debugInfo.push(`  - Created: ${formatTimestamp(task.createdAt)}`);
      if (task.statusMessageTs) {
        debugInfo.push(`  - Status Message: \`${task.statusMessageTs}\``);
      }
    }
  }

  if (inFlightTasks.length > threadTasks.length) {
    debugInfo.push('');
    debugInfo.push(`*Other In-Flight Tasks:* ${inFlightTasks.length - threadTasks.length} task(s) in other threads`);
  }

  await client.chat.postEphemeral({
    channel: channelId,
    user: userId,
    text: debugInfo.join('\n'),
    thread_ts: threadTs,
  });
}

/**
 * Handle "debug logout" command – revokes user authorization.
 */
async function handleDebugLogoutCommand(
  client: WebClient,
  channelId: string,
  userId: string,
  teamId: string,
  threadTs: string,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('handleDebugLogoutCommand');
  logger.info(`debug logout from user ${userId} in thread ${threadTs}`);

  try {
    const isAuthorized = await userAuthService.isUserAuthorized(userId, teamId);

    if (!isAuthorized) {
      await client.chat.postEphemeral({
        channel: channelId,
        user: userId,
        text: '✅ You are already not logged in.',
        thread_ts: threadTs,
      });
      return;
    }

    await userAuthService.revokeUserAuthorization(userId, teamId);

    await client.chat.postEphemeral({
      channel: channelId,
      user: userId,
      text: '✅ Successfully logged out. You will need to authorize again on your next request.',
      thread_ts: threadTs,
    });

    logger.info(`Successfully removed authorization for user ${userId} in team ${teamId}`);
  } catch (error) {
    logger.error(error, `Error handling debug logout command: ${error}`);

    await client.chat.postEphemeral({
      channel: channelId,
      user: userId,
      text: '❌ Failed to log out. Please try again.',
      thread_ts: threadTs,
    });
  }
}

// ---------------------------------------------------------------------------
// Thread history
// ---------------------------------------------------------------------------

interface ThreadHistoryResult {
  historyXml?: string;
  files: SlackFile[];
}

async function fetchThreadHistory(
  client: WebClient,
  channelId: string,
  threadTs: string,
  currentMessageTs: string,
  sinceTs?: string
): Promise<ThreadHistoryResult> {
  const logger = Logger.getLogger('fetchThreadHistory');
  logger.info(`Fetching thread history for channel ${channelId}, thread ${threadTs} since ${sinceTs}`);
  const result: ThreadHistoryResult = { files: [] };

  try {
    const repliesResult = await client.conversations.replies({
      channel: channelId,
      ts: threadTs,
      inclusive: true,
      limit: 100,
    });

    if (!repliesResult.messages || repliesResult.messages.length <= 1) {
      return result;
    }

    // Resolve user names in parallel
    const userIds = new Set<string>();
    for (const msg of repliesResult.messages) {
      if (msg.user) userIds.add(msg.user);
    }

    const userNames = new Map<string, string>();
    await Promise.all(
      Array.from(userIds).map(async (userId) => {
        try {
          const userInfo = await client.users.info({ user: userId });
          if (userInfo.user) {
            const name = userInfo.user.profile?.display_name || userInfo.user.real_name || userInfo.user.name || userId;
            userNames.set(userId, name);
          }
        } catch (e) {
          logger.debug(`Failed to resolve user ${userId}: ${e}`);
        }
      })
    );

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

    const seenFileIds = new Set<string>();

    interface SlackAttachment {
      fallback?: string;
      text?: string;
      pretext?: string;
      title?: string;
      title_link?: string;
      footer?: string;
      fields?: Array<{ title?: string; value?: string }>;
    }

    const historyMessages = repliesResult.messages
      .filter((msg) => {
        if (msg.ts === currentMessageTs) return false;
        if (sinceTs && parseFloat(msg.ts!) <= parseFloat(sinceTs)) return false;
        return true;
      })
      .map((msg) => {
        const msgFiles = (msg as any).files as SlackFile[] | undefined;
        if (msgFiles && msgFiles.length > 0) {
          for (const file of msgFiles) {
            if (!seenFileIds.has(file.id)) {
              seenFileIds.add(file.id);
              result.files.push(file);
            }
          }
        }

        // Resolve <@USERID> mentions to @DisplayName using already-fetched userNames map
        const cleanedText = (msg.text || '')
          .replace(/<@([A-Z0-9]+)>/g, (_, uid) => {
            const name = userNames.get(uid);
            return name ? `@${name}` : '';
          })
          .trim();

        const msgAttachments = (msg as any).attachments as SlackAttachment[] | undefined;
        const attachmentText =
          msgAttachments
            ?.map((a) => {
              const parts: string[] = [];
              if (a.pretext) parts.push(a.pretext);
              if (a.title) parts.push(a.title_link ? `${a.title} (${a.title_link})` : a.title);
              if (a.text) parts.push(a.text);
              if (a.fields && a.fields.length > 0) {
                for (const field of a.fields) {
                  if (field.title && field.value) {
                    parts.push(`${field.title}: ${field.value}`);
                  } else if (field.value) {
                    parts.push(field.value);
                  }
                }
              }
              if (a.footer) parts.push(a.footer);
              if (parts.length === 0 && a.fallback && a.fallback !== '[no preview available]') {
                parts.push(a.fallback);
              }
              return parts.join('\n');
            })
            .filter(Boolean)
            .join('\n\n') || '';

        const fullText = [cleanedText, attachmentText].filter(Boolean).join('\n');

        const msgTimestamp = new Date(parseFloat(msg.ts!) * 1000);
        const isoTimestamp = msgTimestamp.toISOString();
        const relativeTime = formatRelativeTime(msgTimestamp);

        const userName = userNames.get(msg.user!) || '';

        let filesXml = '';
        if (msgFiles && msgFiles.length > 0) {
          const fileElements = msgFiles
            .map((f) => `<file name="${f.name}" type="${f.mimetype}" size="${f.size}" />`)
            .join('');
          filesXml = `\n  <attachedFiles>${fileElements}</attachedFiles>`;
        }

        const role = msg.bot_id ? 'assistant' : 'user';
        const botName = msg.bot_id ? (msg as any).username || 'bot' : '';

        return `<message role="${role}" userId="${msg.user || msg.bot_id}" userName="${userName || botName}" timestamp="${isoTimestamp}" relativeTime="${relativeTime}">${fullText}${filesXml}</message>`;
      })
      .filter((msg): msg is string => msg !== null);

    if (historyMessages.length > 0) {
      result.historyXml = `<thread_context>\n${historyMessages.join('\n')}\n</thread_context>`;
    }

    logger.info(`Fetched ${historyMessages.length} messages from thread history with ${result.files.length} file(s)`);
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
 * Send an ephemeral authorization-required prompt with a button.
 * Works in both channels (ephemeral) and DMs (ephemeral).
 */
async function sendAuthorizationRequired(
  client: WebClient,
  channelId: string,
  userId: string,
  teamId: string,
  threadTs: string,
  messageTs: string,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('sendAuthorizationRequired');
  logger.info(`Sending authorization required message to user ${userId} in channel ${channelId}, thread ${threadTs}`);

  try {
    const state = `slack-auth-${Date.now()}-${userId}`;
    await userAuthService.storeAuthState(state, userId, teamId);

    const config = await import('../../config/config.js').then((m) => m.getConfigFromEnv());
    const appAuthorizeUrl = new URL(`/api/v1/authorize?state=${encodeURIComponent(state)}`, config.baseUrl).toString();

    // Only thread the ephemeral if we're inside an existing thread
    const isInExistingThread = threadTs !== messageTs;

    await client.chat.postEphemeral({
      channel: channelId,
      user: userId,
      text: '❌ You need to authorize the A2A bot first.',
      ...(isInExistingThread && { thread_ts: threadTs }),
      blocks: [
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text: '🔐 *Authorization Required*\n\nYou need to authorize this bot to use A2A services on your behalf.',
          },
        },
        {
          type: 'actions',
          elements: [
            {
              type: 'button',
              text: {
                type: 'plain_text',
                text: 'Authorize Now',
              },
              url: appAuthorizeUrl,
              action_id: 'authorize_button',
              style: 'primary',
            },
          ],
        },
      ],
    });

    logger.info(`Successfully sent ephemeral authorization message`);
  } catch (error) {
    logger.error(error, `Failed to send authorization required message to user ${userId}: ${error}`);
    throw error;
  }
}

// ---------------------------------------------------------------------------
// Unified message handler
// ---------------------------------------------------------------------------

/**
 * Unified handler for both app_mention and direct_message events.
 *
 * This is the single entry-point for processing user messages regardless of
 * whether they come from a channel @mention or a DM.
 */
export async function handleIncomingMessage(msg: NormalizedMessage, deps: HandlerDependencies): Promise<void> {
  const logger = Logger.getLogger('handleIncomingMessage');
  const { userId, teamId, channelId, messageTs, threadTs, rawText, files: eventFiles, dataParts, source, client, appId } = msg;
  const {
    userAuthService,
    a2aClientService,
    contextStore,
    pendingRequestStore,
    inFlightTaskStore,
    baseUrl,
    botToken,
    botName,
    fileStorageService,
    isLocalMode,
  } = deps;

  let statusMessageTs: string | undefined;
  let feedbackRequestData: { sub_agents?: string[] } | null = null;
  let interruptWidgetPosted = false;
  try {
    logger.info(`${source} from user ${userId} in channel ${channelId}`);

    // Post an immediate "Working..." message so the user sees responsiveness
    // before the A2A server sends its first status-update event.
    const statusMessages = {
      thinking: `🧠 ${getSpinnerVerb() || 'Working'}...`,
      activity: '',
      todos: '',
    };
    try {
      const immediateStatus = await client.chat.postMessage({
        channel: channelId,
        thread_ts: threadTs,
        text: statusMessages.thinking,
      });
      statusMessageTs = immediateStatus.ts;
    } catch (err) {
      logger.debug(`Failed to post immediate status message: ${err}`);
    }

    // Resolve <@USERID> mentions to @DisplayName (no-op for DMs without mentions)
    const cleanText = (await resolveMentions(rawText, client)).trim();

    if (!cleanText && (!eventFiles || eventFiles.length === 0) && (!dataParts || dataParts.length === 0)) {
      return;
    }

    // ---- Debug commands (work from both channels and DMs) ----
    if (cleanText.toLowerCase() === 'debug') {
      await handleDebugCommand(
        client,
        channelId,
        userId,
        teamId,
        threadTs,
        messageTs,
        contextStore,
        inFlightTaskStore,
        userAuthService,
        botName
      );
      return;
    }

    if (cleanText.toLowerCase() === 'debug logout') {
      await handleDebugLogoutCommand(client, channelId, userId, teamId, threadTs, userAuthService);
      return;
    }

    // ---- Authorization check ----
    let isAuthorized = false;
    try {
      isAuthorized = await userAuthService.isUserAuthorized(userId, teamId);
    } catch (error: any) {
      if (error.message?.includes('does not exist')) {
        logger.error(error, `Storage configuration error: ${error.message}`);
        await client.chat.postMessage({
          channel: channelId,
          thread_ts: threadTs,
          text: '⚠️ The system is not properly configured. Please contact your administrator.',
        });
        return;
      }
      throw error;
    }

    if (!isAuthorized) {
      logger.info(`User ${userId} is not authorized, will prompt for authorization`);
      await pendingRequestStore.set({
        visitorId: pendingRequestStore.buildVisitorId(teamId, userId),
        text: cleanText,
        channelId,
        threadTs,
        messageTs,
        source,
        appId: msg.appId,
        createdAt: Date.now(),
      });
      await sendAuthorizationRequired(client, channelId, userId, teamId, threadTs, messageTs, userAuthService);
      return;
    }

    // ---- Get orchestrator access token ----
    const accessToken = await userAuthService.getOrchestratorToken(userId, teamId);

    if (!accessToken) {
      logger.error(`Failed to get access token for user ${userId}`);
      await client.chat.postEphemeral({
        channel: channelId,
        user: userId,
        text: '❌ Your authorization has expired. Please authorize again.',
        thread_ts: threadTs,
      });
      await pendingRequestStore.set({
        visitorId: pendingRequestStore.buildVisitorId(teamId, userId),
        text: cleanText,
        channelId,
        threadTs,
        messageTs,
        source,
        appId: msg.appId,
        createdAt: Date.now(),
      });
      await sendAuthorizationRequired(client, channelId, userId, teamId, threadTs, messageTs, userAuthService);
      return;
    }

    // ---- Context & user name resolution ----
    const contextKey = contextStore.buildKey(teamId, channelId, threadTs);
    const existingContext: ContextRecord | null = await contextStore.get(contextKey);
    const existingContextId = existingContext?.contextId;

    let currentUserName = '';
    try {
      const userInfo = await client.users.info({ user: userId });
      currentUserName = userInfo.user?.profile?.display_name || userInfo.user?.real_name || userInfo.user?.name || '';
    } catch (e) {
      logger.debug(`Failed to resolve current user name: ${e}`);
    }

    // ---- Build XML-wrapped request text ----
    const requestTimestamp = new Date(parseFloat(messageTs) * 1000);
    const isoTimestamp = requestTimestamp.toISOString();

    let requestText = cleanText;
    const isInThread = threadTs !== messageTs;
    let threadFiles: SlackFile[] = [];

    const buildAttachedFilesXml = (files: SlackFile[]): string => {
      if (files.length === 0) return '';
      const fileElements = files.map((f) => `<file name="${f.name}" type="${f.mimetype}" size="${f.size}" />`).join('');
      return `\n  <attachedFiles>${fileElements}</attachedFiles>`;
    };

    const currentFilesXml = eventFiles ? buildAttachedFilesXml(eventFiles) : '';

    if (isInThread) {
      const sinceTs = existingContext?.lastProcessedTs;
      const threadHistoryResult = await fetchThreadHistory(client, channelId, threadTs, messageTs, sinceTs);
      threadFiles = threadHistoryResult.files;

      if (threadHistoryResult.historyXml) {
        requestText = `${threadHistoryResult.historyXml}\n<current_request userId="${userId}" userName="${currentUserName}" timestamp="${isoTimestamp}">${cleanText}${currentFilesXml}</current_request>`;
        logger.info(
          `Included thread history ${sinceTs ? 'since last interaction' : '(full)'} with ${threadFiles.length} file(s)`
        );
      } else {
        requestText = `<message role="user" userId="${userId}" userName="${currentUserName}" timestamp="${isoTimestamp}">${cleanText}${currentFilesXml}</message>`;
      }
    } else {
      requestText = `<message role="user" userId="${userId}" userName="${currentUserName}" timestamp="${isoTimestamp}">${cleanText}${currentFilesXml}</message>`;
    }

    // ---- Process files (current message + thread history, deduplicated) ----
    const webhookUrl = new URL(`/api/v1/a2a/callback`, baseUrl).toString();
    const webhookToken = randomUUID();

    const seenFileIds = new Set<string>();
    const allFiles: SlackFile[] = [];

    if (eventFiles && eventFiles.length > 0) {
      for (const file of eventFiles) {
        if (!seenFileIds.has(file.id)) {
          seenFileIds.add(file.id);
          allFiles.push(file);
        }
      }
    }
    for (const file of threadFiles) {
      if (!seenFileIds.has(file.id)) {
        seenFileIds.add(file.id);
        allFiles.push(file);
      }
    }

    let processedFiles: Awaited<ReturnType<typeof processSlackFilesToS3>> = [];

    if (allFiles.length > 0) {
      const currentFileCount = eventFiles?.length || 0;
      const historyFileCount = allFiles.length - currentFileCount;
      logger.info(
        `Processing ${allFiles.length} file(s): ${currentFileCount} from current message, ${historyFileCount} from thread history`
      );

      const warnings = getFileProcessingWarnings(allFiles);
      if (warnings.length > 0 && !hasProcessableFiles(allFiles)) {
        await client.chat.postEphemeral({
          channel: channelId,
          user: userId,
          text: `⚠️ Could not process attached files:\n${warnings.join('\n')}`,
          thread_ts: threadTs,
        });
      } else if (warnings.length > 0) {
        await client.chat.postEphemeral({
          channel: channelId,
          user: userId,
          text: `⚠️ Some files could not be processed:\n${warnings.join('\n')}`,
          thread_ts: threadTs,
        });
      }

      processedFiles = await processSlackFilesToS3(allFiles, botToken, fileStorageService, userId, threadTs);
      logger.info(`Successfully processed ${processedFiles.length} of ${allFiles.length} file(s) to S3`);
    }

    // ---- Build & send A2A request via streaming ----
    const a2aRequest: A2ASlackBasedRequest = {
      userId,
      teamId,
      channelId,
      threadTs: isInThread ? threadTs : undefined,
      messageTs,
      text: requestText,
      fileUrls:
        processedFiles.length > 0
          ? processedFiles.map((f) => ({
              name: f.name,
              mimeType: f.mimeType,
              url: f.url,
            }))
          : undefined,
      dataParts,
      contextId: existingContextId || undefined,
      webhookUrl: isLocalMode ? undefined : webhookUrl,
      webhookToken: isLocalMode ? undefined : webhookToken,
    };

    logger.info('Sending message via streaming');

    let accumulatedTask: Task | null = null;
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
            visitorId: inFlightTaskStore.buildVisitorId(teamId, userId),
            userId,
            teamId,
            channelId,
            threadTs,
            messageTs,
            statusMessageTs,
            contextKey,
            webhookToken,
            source,
            appId,
            createdAt: Date.now(),
          });

          // Store context ID if we got one
          await contextStore.set(contextKey, accumulatedTask.contextId ?? '', messageTs);
        } else if (event.kind === 'message') {
          const message = event as Message;
          logger.debug({ taskId: message.taskId, message }, `Stream message received. Doing nothing.`);
        } else if (!accumulatedTask) {
          logger.debug(`Received ${_.get(event, 'kind')} before task. Bug in this app or A2A server? Ignoring.`);
        } else if (event.kind === 'status-update') {
          const statusEvent = event as TaskStatusUpdateEvent;

          // Update final response state
          accumulatedTask.status = statusEvent.status;

          // Handle interrupted states (input-required) with HITL extension
          if (statusEvent.status.state === 'input-required' && statusEvent.status.message) {
            const extensions = statusEvent.status.message.extensions || [];
            const isHitlInterrupt = extensions.includes('urn:nannos:a2a:human-in-the-loop:1.0');
            
            if (isHitlInterrupt) {
              logger.info(
                { taskId: accumulatedTask?.id },
                `Received HITL interrupt via extension`
              );
              
              // Extract text description from TextPart and structured data from DataPart
              let interruptMessage = '';
              let actionRequests: any[] = [];
              if (statusEvent.status.message?.parts) {
                for (const part of statusEvent.status.message.parts) {
                  if (part.kind === 'text') {
                    interruptMessage += (part as { kind: 'text'; text: string }).text;
                  } else if (part.kind === 'data') {
                    const data = (part as { kind: 'data'; data: any }).data;
                    if (data?.action_requests) {
                      actionRequests = data.action_requests;
                    }
                  }
                }
              }

              if (interruptMessage) {
                // Determine interrupt type from action_requests
                const toolNames = actionRequests.map((ar: any) => ar?.name).filter(Boolean);
                const isBugReport = toolNames.includes('console_create_bug_report');

                if (isBugReport) {
                  // Mark as handled regardless of widget success/failure to prevent duplicate from handleTask
                  interruptWidgetPosted = true;
                  const { buildBugReportWidget } = await import('../../utils/taskResponseHandler.js');
                  
                  try {
                    const interruptReason = actionRequests.find((ar: any) => ar.name === 'console_create_bug_report')?.args?.description || interruptMessage;
                    const bugReportWidget = buildBugReportWidget({
                      taskId: accumulatedTask.id,
                      contextId: accumulatedTask.contextId || '',
                      reason: interruptReason,
                      channelId,
                      threadTs,
                      actionRequests,
                    });

                    logger.info(
                      { taskId: accumulatedTask?.id },
                      `Posting bug report widget to Slack`
                    );

                    await client.chat.postMessage({
                      channel: channelId,
                      thread_ts: threadTs,
                      text: `🐛 Bug Report`,
                      blocks: bugReportWidget,
                    });
                  } catch (widgetErr) {
                    logger.error(widgetErr, `Failed to post bug report widget, falling back to text: ${widgetErr}`);
                    await postMessage(client, channelId, threadTs, interruptMessage);
                  }
                } else {
                  // For other HITL interrupt types, post message with tool info
                  logger.info(
                    { taskId: accumulatedTask?.id, toolNames },
                    `Posting HITL interrupt message to Slack`
                  );
                  await postMessage(client, channelId, threadTs, interruptMessage);
                }

                // Store the interrupt context so we can resume later
                await inFlightTaskStore.touch(accumulatedTask.id).catch((err) => {
                  logger.error(err, `Failed to update in-flight task for interrupt: ${err}`);
                });
              }
            } else if (statusEvent.status.message?.metadata) {
              // Legacy non-extension interrupt handling (e.g., auth_required)
              const interruptType = (statusEvent.status.message.metadata as any).interrupt_type;
              if (interruptType) {
                let interruptMessage = '';
                if (statusEvent.status.message?.parts) {
                  for (const part of statusEvent.status.message.parts) {
                    if (part.kind === 'text') {
                      interruptMessage += (part as { kind: 'text'; text: string }).text;
                    }
                  }
                }
                if (interruptMessage) {
                  await postMessage(client, channelId, threadTs, interruptMessage);
                }
              }
            }
          }

          if (event.status.message?.extensions?.includes('urn:nannos:a2a:work-plan:1.0')) {
            const workPlan = event.status.message.parts.find((x) => x.kind === 'data')?.data as {
              todos: Array<{
                name: string; // Required: task description
                state: 'submitted' | 'working' | 'completed' | 'failed'; // Required: current state
                source?: string; // Optional: agent that owns this todo
                target?: string; // Optional: resource ID being operated on
              }>;
            };
            statusMessages.todos = workPlan.todos
              .map(
                (x) =>
                  `• ${x.state === 'completed' ? '✅' : x.state === 'working' ? '⏳' : x.state === 'failed' ? '❌' : '🔜'} ${x.name}${x.source ? ` (agent ${x.source})` : ''}${x.target ? ` [${x.target}]` : ''}`
              )
              .join('\n');
          } else if (event.status.message?.extensions?.includes('urn:nannos:a2a:activity-log:1.0')) {
            statusMessages.activity = event.status.message.parts.find((x) => x.kind === 'text')?.text || '';
          } else if (event.status.message?.extensions?.includes('urn:nannos:a2a:intermediate-output:1.0')) {
            logger.debug(
              `Received status update as intermediate-output. Not updating status message details. This is thinking`
            );
          } else if (event.status.message?.extensions?.includes('urn:nannos:a2a:feedback-request:1.0')) {
            // Store feedback request data to send as ephemeral after final response
            const feedbackData = event.status.message.parts.find((x) => x.kind === 'data')?.data as {
              sub_agents?: string[];
            };
            logger.debug({ feedbackData }, `Received feedback-request extension`);
            feedbackRequestData = feedbackData;
          } else if (statusEvent.status.state !== 'input-required') {
            // Only log if it's not an interrupt (interrupts already logged above)
            logger.debug(
              `Received status update without recognized extensions (${event.status.message?.extensions}). Not updating status message details.`
            );
          }
          
          const newStatustMessage = `${statusMessages.thinking}${statusMessages.activity ? ` [${statusMessages.activity}]` : ''}${statusMessages.todos ? `\n${statusMessages.todos}` : ''}`;
          if (statusMessageTs) {
            await client.chat.update({
              channel: channelId,
              ts: statusMessageTs,
              markdown_text: newStatustMessage,
            });
          }
        } else if (event.kind === 'artifact-update') {
          if (!accumulatedTask.artifacts) accumulatedTask.artifacts = [];
          accumulatedTask.artifacts.push(event.artifact);
          logger.debug(
            { taskId: accumulatedTask?.id, accumulatedTask },
            `Building artifact update. Total artifacts now: ${accumulatedTask.artifacts.length}`
          );
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
    // Skip if we already posted a custom interrupt widget (e.g. bug report)
    if (interruptWidgetPosted) {
      logger.info({ taskId: accumulatedTask?.id }, `Skipping handleTask — interrupt widget already posted`);
      return;
    }
    const result = await handleTask({
      task: accumulatedTask,
      slackClient: client,
      messageContext: {
        channelId,
        threadTs,
        messageTs,
        statusMessageTs,
      },
    });
    if (result.messageTs) {
      contextStore.set(contextKey, accumulatedTask?.contextId, result.messageTs).catch((err) => {
        logger.error(err, `Failed to update context store for task ${accumulatedTask?.id}: ${err}`);
      });

      // Store response mapping so emoji reactions can be correlated to A2A IDs
      if (deps.feedbackService && accumulatedTask.contextId) {
        deps.feedbackService.responseMapping.set(channelId, result.messageTs, {
          contextId: accumulatedTask.contextId,
          taskId: accumulatedTask.id,
          userId,
          teamId,
          createdAt: Date.now(),
        });
        logger.info(`Stored response mapping for feedback: channel=${channelId}, messageTs=${result.messageTs}, contextId=${accumulatedTask.contextId}, taskId=${accumulatedTask.id}`);
      } else {
        logger.debug(`Not storing response mapping: feedbackService=${!!deps.feedbackService}, contextId=${accumulatedTask.contextId}`);
      }

      // Send ephemeral feedback widget if feedback was requested
      if (feedbackRequestData && deps.feedbackService) {
        // Encode context/task IDs + sub_agents in button values
        const contextId = accumulatedTask.contextId || '';
        const taskId = accumulatedTask.id || '';
        const subAgents = feedbackRequestData.sub_agents || [];
        const encodedValue = Buffer.from(
          JSON.stringify({ contextId, taskId, userId, teamId, subAgents })
        ).toString('base64');

        const blocks: any[] = [
          {
            type: 'section',
            text: {
              type: 'mrkdwn',
              text: '👋 Was this response helpful?',
            },
          },
          {
            type: 'actions',
            elements: [
              {
                type: 'button',
                text: {
                  type: 'plain_text',
                  text: '👍 Yes',
                  emoji: true,
                },
                value: encodedValue,
                action_id: 'feedback_thumbsup',
                style: 'primary',
              },
              {
                type: 'button',
                text: {
                  type: 'plain_text',
                  text: '👎 No',
                  emoji: true,
                },
                value: encodedValue,
                action_id: 'feedback_thumbsdown',
                style: 'danger',
              },
            ],
          },
        ];

        // Add sub-agents attribution if available
        if (feedbackRequestData.sub_agents && feedbackRequestData.sub_agents.length > 0) {
          blocks.push({
            type: 'context',
            elements: [
              {
                type: 'mrkdwn',
                text: `_Agents involved: ${feedbackRequestData.sub_agents.join(', ')}_`,
              },
            ],
          });
        }

        try {
          await client.chat.postEphemeral({
            channel: channelId,
            user: userId,
            ...(threadTs && { thread_ts: threadTs }),
            blocks: blocks,
          });
          logger.info(`Sent feedback ephemeral message to user ${userId}`);
        } catch (err) {
          logger.error(err, `Failed to send feedback ephemeral message: ${err}`);
        }
      } else if (feedbackRequestData) {
        logger.warn(
          `Feedback widget requested but feedbackService is not available. Set CONSOLE_BACKEND_URL environment variable to enable feedback functionality.`
        );
      }
    }
  } catch (error) {
    logger.error(error, `Error handling ${source}: ${error}`);
    await handleError(client, channelId, threadTs, messageTs);
  } finally {
    // Clean up thinking message if it still exists (e.g. if A2A server didn't send any status updates or final response)
    if (statusMessageTs) {
      try {
        await client.chat.delete({
          channel: channelId,
          ts: statusMessageTs,
        });
      } catch (err) {
        logger.trace(err, `Failed to delete thinking message: ${err}`);
      }
    }
  }
}
