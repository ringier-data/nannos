import { Logger } from '../utils/logger.js';

import type { IContextStore, IInFlightTaskStore } from '../storage/types.js';
import { UserAuthService } from '../services/userAuthService.js';
import { GoogleChatService } from '../services/googleChatService.js';
import { HandlerDependencies } from './types.js';

function formatTimestamp(ts: number): string {
  return new Date(ts).toISOString();
}

// ---------------------------------------------------------------------------
// Debug commands
// ---------------------------------------------------------------------------
/**
 * Handle "debug" command – shows thread context, in-flight tasks, auth status.
 * Google Chat has no ephemeral messages, so we send a regular message in the thread.
 */
async function handleDebugCommand(
  chatService: GoogleChatService,
  spaceId: string,
  userId: string,
  projectId: string,
  threadId: string,
  _messageId: string,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('handleDebugCommand');
  logger.info(`debug command from user ${userId} in thread ${threadId}`);

  const debugInfo: string[] = [];
  debugInfo.push('🔍 *Nannos Debug Information*\n');

  debugInfo.push('*Identifiers:*');
  debugInfo.push(`• Project ID: \`${projectId}\``);
  debugInfo.push(`• Space ID: \`${spaceId}\``);
  debugInfo.push(`• User ID: \`${userId}\``);
  debugInfo.push(`• Thread ID: \`${threadId}\``);
  debugInfo.push('');

  const isAuthorized = await userAuthService.isUserAuthorized(userId, projectId);
  debugInfo.push('*Login Status:*');
  debugInfo.push(`• Status: ${isAuthorized ? '✅ Logged in' : '❌ Not logged in'}`);
  debugInfo.push('');

  const contextKey = contextStore.buildKey(projectId, spaceId, threadId);
  const contextRecord = await contextStore.get(contextKey);

  debugInfo.push('*Thread Context:*');
  debugInfo.push(`• Context Key: \`${contextKey}\``);
  if (contextRecord) {
    debugInfo.push(`• Context ID: \`${contextRecord.contextId}\``);
    if (contextRecord.lastProcessedMessageId) {
      debugInfo.push(`• Last Processed Message: \`${contextRecord.lastProcessedMessageId}\``);
    }
  } else {
    debugInfo.push('• No context found for this thread');
  }
  debugInfo.push('');

  const inFlightTasks = await inFlightTaskStore.getByUser(projectId, userId);
  const threadTasks = inFlightTasks.filter((t) => t.threadId === threadId);

  debugInfo.push('*In-Flight Tasks (this thread):*');
  if (threadTasks.length === 0) {
    debugInfo.push('• No in-flight tasks in this thread');
  } else {
    for (const task of threadTasks) {
      debugInfo.push(`• Task ID: \`${task.taskId}\``);
      debugInfo.push(`  - Source: ${task.source}`);
      debugInfo.push(`  - Created: ${formatTimestamp(task.createdAt)}`);
      if (task.statusMessageId) {
        debugInfo.push(`  - Status Message: \`${task.statusMessageId}\``);
      }
    }
  }

  if (inFlightTasks.length > threadTasks.length) {
    debugInfo.push('');
    debugInfo.push(`*Other In-Flight Tasks:* ${inFlightTasks.length - threadTasks.length} task(s) in other threads`);
  }

  await chatService.sendPrivateTextMessage(projectId, spaceId, userId, debugInfo.join('\n'), threadId);
}

/**
 * Handle "debug logout" command – revokes user authorization.
 */
async function handleDebugLogoutCommand(
  chatService: GoogleChatService,
  spaceId: string,
  userId: string,
  projectId: string,
  threadId: string,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('handleDebugLogoutCommand');
  logger.info(`debug logout from user ${userId} in thread ${threadId}`);

  try {
    const isAuthorized = await userAuthService.isUserAuthorized(userId, projectId);

    if (!isAuthorized) {
      await chatService.sendPrivateTextMessage(
        projectId,
        spaceId,
        userId,
        '✅ You are already not logged in to Nannos.',
        threadId
      );
      return;
    }

    await userAuthService.revokeUserAuthorization(userId, projectId);

    await chatService.sendPrivateTextMessage(
      projectId,
      spaceId,
      userId,
      '✅ Successfully logged out from Nannos. You will need to authorize again on your next request.',
      threadId
    );

    logger.info(`Successfully removed authorization for user ${userId} in project ${projectId}`);
  } catch (error) {
    logger.error(error, `Error handling debug logout command: ${error}`);

    await chatService.sendPrivateTextMessage(
      projectId,
      spaceId,
      userId,
      '❌ Failed to log out. Please try again.',
      threadId
    );
  }
}

/**
 * Handle "login" command – sends an authorization card.
 */
async function handleLoginCommand(
  chatService: GoogleChatService,
  spaceId: string,
  userId: string,
  projectId: string,
  threadId: string,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('handleLoginCommand');
  logger.info(`login command from user ${userId}`);

  const isAuthorized = await userAuthService.isUserAuthorized(userId, projectId);

  if (isAuthorized) {
    await chatService.sendPrivateTextMessage(projectId, spaceId, userId, '✅ You are already logged in!', threadId);
    return;
  }

  const state = `gchat-auth-${Date.now()}-${userId}`;
  await userAuthService.storeAuthState(state, userId, projectId);

  const config = await import('../config/config.js').then((m) => m.getConfigFromEnv());
  const appAuthorizeUrl = new URL(`/api/v1/authorize?state=${encodeURIComponent(state)}`, config.baseUrl).toString();

  // Send a card with the authorize button
  const card = chatService.buildAuthCard(
    appAuthorizeUrl,
    '🔐 Login Required',
    'To use Nannos, you need to log in with your account.\nClick the button below to start the login process.',
    'Log In'
  );

  await chatService.sendPrivateCardMessage(projectId, spaceId, userId, [card], threadId);
}

/**
 * Handle "help" command – shows available commands.
 */
async function handleHelpCommand(
  chatService: GoogleChatService,
  spaceId: string,
  userId: string,
  projectId: string,
  threadId: string
): Promise<void> {
  const helpText = [
    '🤖 *Nannos Commands*\n',
    '• *login* - Log in to use Nannos services',
    '• *debug* - Show debug info about your session and threads',
    '• *logout* - Log out from Nannos',
    '• *help* - Show this help message',
    '',
    'You can also just send a message to ask Nannos anything!',
  ].join('\n');

  await chatService.sendPrivateTextMessage(projectId, spaceId, userId, helpText, threadId);
}

// ---------------------------------------------------------------------------
// Unified command dispatcher (slash commands & quick commands)
// ---------------------------------------------------------------------------

/**
 * Dispatch a slash or quick command by its configured command ID.
 *
 * @see https://developers.google.com/workspace/chat/commands
 */
export async function handleAppCommand(
  commandArgument: string,
  spaceId: string,
  userId: string,
  projectId: string,
  threadId: string,
  messageId: string,
  deps: HandlerDependencies
): Promise<void> {
  const { chatService, contextStore, inFlightTaskStore, userAuthService } = deps;

  const logger = Logger.getLogger('handleAppCommand');
  logger.info(`App command argument=${commandArgument} from user ${userId} in space ${spaceId}`);

  switch (commandArgument) {
    case 'help':
      return handleHelpCommand(chatService, spaceId, userId, projectId, threadId);

    case 'login':
      return handleLoginCommand(chatService, spaceId, userId, projectId, threadId, userAuthService);

    case 'debug':
      return handleDebugCommand(
        chatService,
        spaceId,
        userId,
        projectId,
        threadId,
        messageId,
        contextStore,
        inFlightTaskStore,
        userAuthService
      );

    case 'logout':
      return handleDebugLogoutCommand(chatService, spaceId, userId, projectId, threadId, userAuthService);

    default:
      logger.warn(`Unknown command argument=${commandArgument}, ignoring`);
  }
}
