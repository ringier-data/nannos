import { Logger } from '../utils/logger.js';

import type { IContextStore, IInFlightTaskStore, InFlightTask } from '../storage/types.js';
import { UserAuthService } from '../services/userAuthService.js';
import { GoogleChatService } from '../services/googleChatService.js';
import { A2AClientService } from '../services/a2aClientService.js';
import { HandlerDependencies } from './types.js';

// A2A JSON-RPC error codes signalling the task is already terminal (or gone),
// i.e. the stored in-flight record is stale and can be dropped.
const A2A_TASK_NOT_FOUND = -32001;
const A2A_TASK_NOT_CANCELABLE = -32002;


export interface AppCommand {
  commandArgument: string;
  spaceId: string;
  userId: string;
  projectId: string;
  threadId: string;
  messageId: string;
};

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
  appCommand: AppCommand,
  chatService: GoogleChatService,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('handleDebugCommand');
  logger.info(`debug command from user ${appCommand.userId} in thread ${appCommand.threadId}`);

  const debugInfo: string[] = [];
  debugInfo.push('🔍 *Nannos Debug Information*\n');

  debugInfo.push('*Identifiers:*');
  debugInfo.push(`• Project ID: \`${appCommand.projectId}\``);
  debugInfo.push(`• Space ID: \`${appCommand.spaceId}\``);
  debugInfo.push(`• User ID: \`${appCommand.userId}\``);
  debugInfo.push(`• Thread ID: \`${appCommand.threadId}\``);
  debugInfo.push('');

  const isAuthorized = await userAuthService.isUserAuthorized(appCommand.userId, appCommand.projectId);
  debugInfo.push('*Login Status:*');
  debugInfo.push(`• Status: ${isAuthorized ? '✅ Logged in' : '❌ Not logged in'}`);
  debugInfo.push('');

  const contextKey = contextStore.buildKey(appCommand.projectId, appCommand.spaceId, appCommand.threadId);
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

  const inFlightTasks = await inFlightTaskStore.getByUser(appCommand.projectId, appCommand.userId);
  const threadTasks = inFlightTasks.filter((t) => t.threadId === appCommand.threadId);

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

  await chatService.sendPrivateTextMessage(appCommand.projectId, appCommand.spaceId, appCommand.userId, debugInfo.join('\n'), appCommand.threadId);
}

/**
 * Handle "debug logout" command – revokes user authorization.
 */
async function handleDebugLogoutCommand(
  appCommand: AppCommand,
  chatService: GoogleChatService,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('handleDebugLogoutCommand');
  logger.info(`debug logout from user ${appCommand.userId} in thread ${appCommand.threadId}`);

  try {
    const isAuthorized = await userAuthService.isUserAuthorized(appCommand.userId, appCommand.projectId);

    if (!isAuthorized) {
      await chatService.sendPrivateTextMessage(
        appCommand.projectId,
        appCommand.spaceId,
        appCommand.userId,
        '✅ You are already not logged in to Nannos.',
        appCommand.threadId
      );
      return;
    }

    await userAuthService.revokeUserAuthorization(appCommand.userId, appCommand.projectId);

    await chatService.sendPrivateTextMessage(
      appCommand.projectId,
      appCommand.spaceId,
      appCommand.userId,
      '✅ Successfully logged out from Nannos. You will need to authorize again on your next request.',
      appCommand.threadId
    );

    logger.info(`Successfully removed authorization for user ${appCommand.userId} in project ${appCommand.projectId}`);
  } catch (error) {
    logger.error(error, `Error handling debug logout command: ${error}`);

    await chatService.sendPrivateTextMessage(
      appCommand.projectId,
      appCommand.spaceId,
      appCommand.userId,
      '❌ Failed to log out. Please try again.',
      appCommand.threadId
    );
  }
}

/**
 * Handle "login" command – sends an authorization card.
 */
async function handleLoginCommand(
  appCommand: AppCommand,
  chatService: GoogleChatService,
  userAuthService: UserAuthService
): Promise<void> {
  const logger = Logger.getLogger('handleLoginCommand');
  logger.info(`login command from user ${appCommand.userId}`);

  const isAuthorized = await userAuthService.isUserAuthorized(appCommand.userId, appCommand.projectId);

  if (isAuthorized) {
    await chatService.sendPrivateTextMessage(appCommand.projectId, appCommand.spaceId, appCommand.userId, '✅ You are already logged in!', appCommand.threadId);
    return;
  }

  const state = `gchat-auth-${Date.now()}-${appCommand.userId}`;
  await userAuthService.storeAuthState(state, appCommand.userId, appCommand.projectId);

  const config = await import('../config/config.js').then((m) => m.getConfigFromEnv());
  const appAuthorizeUrl = new URL(`/api/v1/authorize?state=${encodeURIComponent(state)}`, config.baseUrl).toString();

  // Send a card with the authorize button
  const card = chatService.buildAuthCard(
    appAuthorizeUrl,
    '🔐 Login Required',
    'To use Nannos, you need to log in with your account.\nClick the button below to start the login process.',
    'Log In'
  );

  await chatService.sendPrivateCardMessage(appCommand.projectId, appCommand.spaceId, appCommand.userId, [card], appCommand.threadId);
}

/**
 * Handle "cancel" command – aborts a running task via the same A2A cancel
 * protocol the web client's stop button uses.
 *
 * The task is resolved from the current thread when possible; otherwise from
 * the user's in-flight tasks (a task ID argument disambiguates).
 */
export async function handleCancelCommand(
  appCommand: AppCommand,
  args: string,
  chatService: GoogleChatService,
  inFlightTaskStore: IInFlightTaskStore,
  userAuthService: UserAuthService,
  a2aClientService: A2AClientService
): Promise<void> {
  const logger = Logger.getLogger('handleCancelCommand');
  const { projectId, spaceId, userId, threadId } = appCommand;

  logger.info(`cancel command from user ${userId} in thread ${threadId}`);

  const reply = (text: string) => chatService.sendPrivateTextMessage(projectId, spaceId, userId, text, threadId);

  const tasks = await inFlightTaskStore.getByUser(projectId, userId);

  if (tasks.length === 0) {
    await reply('ℹ️ You have no running tasks to cancel.');
    return;
  }

  const requestedTaskId = args.trim();
  const threadTasks = tasks.filter((t) => t.threadId === threadId);
  let task: InFlightTask | undefined;

  if (requestedTaskId) {
    task = tasks.find((t) => t.taskId === requestedTaskId || t.taskId.startsWith(requestedTaskId));
    if (!task) {
      await reply(
        [
          `❓ No running task matches \`${requestedTaskId}\`.`,
          '',
          '*Your running tasks:*',
          ...tasks.map((t) => `• \`${t.taskId}\` — started ${formatTimestamp(t.createdAt)}`),
        ].join('\n')
      );
      return;
    }
  } else if (threadTasks.length === 1) {
    task = threadTasks[0];
  } else if (threadTasks.length === 0 && tasks.length === 1) {
    task = tasks[0];
  } else {
    const candidates = threadTasks.length > 0 ? threadTasks : tasks;
    await reply(
      [
        `You have ${candidates.length} running tasks. Specify which one to cancel:`,
        '',
        ...candidates.map((t) => `• \`${t.taskId}\` — started ${formatTimestamp(t.createdAt)}`),
        '',
        'Use the *cancel* command followed by the task ID.',
      ].join('\n')
    );
    return;
  }

  const accessToken = await userAuthService.getOrchestratorToken(userId, projectId);
  if (!accessToken) {
    await reply('🔐 You need to log in first. Use the *login* command.');
    return;
  }

  const response = await a2aClientService.cancelTask(task.taskId, accessToken);

  if ('error' in response && response.error) {
    if (response.error.code === A2A_TASK_NOT_FOUND || response.error.code === A2A_TASK_NOT_CANCELABLE) {
      // Stale record — the task already reached a terminal state.
      await inFlightTaskStore.delete(task.taskId).catch(() => {});
      await reply(`ℹ️ Task \`${task.taskId}\` has already finished — nothing to cancel.`);
    } else {
      await reply(`❌ Failed to cancel task \`${task.taskId}\`: ${response.error.message}`);
    }
    return;
  }

  await reply(`🛑 Cancellation requested for task \`${task.taskId}\`. The task will stop shortly.`);
}

/**
 * Handle "help" command – shows available commands.
 */
async function handleHelpCommand(
  appCommand: AppCommand,
  chatService: GoogleChatService,
): Promise<void> {
  const helpText = [
    '🤖 *Nannos Commands*\n',
    '• *login* - Log in to use Nannos services',
    '• *cancel [task_id]* - Cancel a running task (alias: *stop*)',
    '• *debug* - Show debug info about your session and threads',
    '• *logout* - Log out from Nannos',
    '• *help* - Show this help message',
    '',
    'You can also just send a message to ask Nannos anything!',
  ].join('\n');

  await chatService.sendPrivateTextMessage(appCommand.projectId, appCommand.spaceId, appCommand.userId, helpText, appCommand.threadId);
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
  appCommand: AppCommand,
  deps: HandlerDependencies
): Promise<void> {
  const { chatService, contextStore, inFlightTaskStore, userAuthService, a2aClientService } = deps;

  const logger = Logger.getLogger('handleAppCommand');
  logger.info(`App command argument=${appCommand.commandArgument} from user ${appCommand.userId} in space ${appCommand.spaceId}`);

  // Dispatch on the first token so commands can take arguments (e.g. "cancel <task_id>")
  const [commandName, ...commandArgs] = appCommand.commandArgument.split(/\s+/);
  const commandArgsText = commandArgs.join(' ');

  switch (commandName) {
    case 'help':
      return handleHelpCommand(appCommand, chatService);

    case 'login':
      return handleLoginCommand(appCommand, chatService, userAuthService);

    case 'cancel':
    case 'stop':
      return handleCancelCommand(
        appCommand,
        commandArgsText,
        chatService,
        inFlightTaskStore,
        userAuthService,
        a2aClientService
      );

    case 'debug':
      return handleDebugCommand(
        appCommand,
        chatService,
        contextStore,
        inFlightTaskStore,
        userAuthService
      );

    case 'logout':
      return handleDebugLogoutCommand(appCommand, chatService, userAuthService);

    default:
      logger.warn(`Unknown command argument=${appCommand.commandArgument}, ignoring`);
  }
}
