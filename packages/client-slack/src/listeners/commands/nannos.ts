import { App, SlackCommandMiddlewareArgs, AllMiddlewareArgs } from '@slack/bolt';
import { UserAuthService } from '../../services/userAuthService.js';
import type { IContextStore, IInFlightTaskStore, IPendingRequestStore, IOAuthStateStore } from '../../storage/types.js';
import { Logger } from '../../utils/logger.js';

type NannosCommand = SlackCommandMiddlewareArgs & AllMiddlewareArgs;

/**
 * Format timestamp for display
 */
function formatTimestamp(ts: number): string {
  return new Date(ts).toISOString();
}

/**
 * Format Slack timestamp to readable date
 */
function formatSlackTs(slackTs: string): string {
  const epochSeconds = parseFloat(slackTs);
  return new Date(epochSeconds * 1000).toISOString();
}

/**
 * Handle /bot login subcommand
 */
async function handleLoginSubcommand(
  { command, respond, client }: NannosCommand,
  userAuthService: UserAuthService,
  botName: string
): Promise<void> {
  const logger = Logger.getLogger('handleLoginSubcommand');
  const userId = command.user_id;
  const teamId = command.team_id;

  logger.info(`${command.command} login from user ${userId} in team ${teamId}`);

  // Check if user is already authorized
  const isAuthorized = await userAuthService.isUserAuthorized(userId, teamId);

  if (isAuthorized) {
    await respond({
      text: '✅ You are already logged in!',
    });
    return;
  }

  // Generate state for authorization flow
  const state = `slack-auth-${Date.now()}-${userId}`;

  // Store state for later retrieval
  await userAuthService.storeAuthState(state, userId, teamId);

  // Build app's authorize endpoint URL
  const config = await import('../../config/config.js').then((m) => m.getConfigFromEnv());
  const appAuthorizeUrl = new URL(`/api/v1/authorize?state=${encodeURIComponent(state)}`, config.baseUrl).toString();

  // Send DM with authorization link
  const dmResult = await client.conversations.open({ users: userId });

  if (dmResult.ok && dmResult.channel?.id) {
    await client.chat.postMessage({
      channel: dmResult.channel.id,
      text: '🔐 *Login Required*',
      blocks: [
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text: `🔐 *Login Required*\n\nTo use ${botName}, you need to log in with your account.`,
          },
        },
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text: 'Click the button below to start the login process:',
          },
        },
        {
          type: 'actions',
          elements: [
            {
              type: 'button',
              text: {
                type: 'plain_text',
                text: 'Log In',
                emoji: true,
              },
              url: appAuthorizeUrl,
              style: 'primary',
              action_id: 'authorize_button',
            },
          ],
        },
        {
          type: 'context',
          elements: [
            {
              type: 'mrkdwn',
              text: '_You will be redirected to our identity provider to complete login._',
            },
          ],
        },
      ],
    });

    await respond({
      text: "📩 I've sent you a DM with login instructions.",
    });
  } else {
    // Fallback: post authorization link in channel (ephemeral)
    await respond({
      text: '🔐 *Login Required*',
      blocks: [
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text: '🔐 *Login Required*\n\nClick the button below to log in:',
          },
        },
        {
          type: 'actions',
          elements: [
            {
              type: 'button',
              text: {
                type: 'plain_text',
                text: 'Log In',
              },
              url: appAuthorizeUrl,
              style: 'primary',
              action_id: 'authorize_button',
            },
          ],
        },
      ],
    });
  }
}

/**
 * Handle /bot debug subcommand
 */
async function handleDebugSubcommand(
  { command, respond }: NannosCommand,
  userAuthService: UserAuthService,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore,
  pendingRequestStore: IPendingRequestStore,
  args: string,
  botName: string
): Promise<void> {
  const logger = Logger.getLogger('handleDebugSubcommand');
  const userId = command.user_id;
  const teamId = command.team_id;
  const channelId = command.channel_id;

  logger.info(`${command.command} debug from user ${userId} in channel ${channelId}`);

  const debugInfo: string[] = [];
  debugInfo.push(`🔍 *${botName} Debug Information*\n`);

  // Basic identifiers
  debugInfo.push('*Identifiers:*');
  debugInfo.push(`• Team ID: \`${teamId}\``);
  debugInfo.push(`• Channel ID: \`${channelId}\``);
  debugInfo.push(`• User ID: \`${userId}\``);
  debugInfo.push('');

  // Check user authorization status
  const isAuthorized = await userAuthService.isUserAuthorized(userId, teamId);
  debugInfo.push('*Login Status:*');
  debugInfo.push(`• Status: ${isAuthorized ? '✅ Logged in' : '❌ Not logged in'}`);
  debugInfo.push('');

  // Check for pending requests
  const visitorId = pendingRequestStore.buildVisitorId(teamId, userId);
  debugInfo.push('*Pending Request:*');
  debugInfo.push(`• Visitor ID: \`${visitorId}\``);
  debugInfo.push(`• _Use ${command.command} login if you have a pending request_`);
  debugInfo.push('');

  // Check for in-flight tasks for this user
  const inFlightTasks = await inFlightTaskStore.getByUser(teamId, userId);
  debugInfo.push('*In-Flight Tasks:*');
  if (inFlightTasks.length === 0) {
    debugInfo.push('• No in-flight tasks');
  } else {
    for (const task of inFlightTasks) {
      debugInfo.push(`• Task ID: \`${task.taskId}\``);
      debugInfo.push(`  - Channel: \`${task.channelId}\``);
      debugInfo.push(`  - Thread: \`${task.threadTs}\` (${formatSlackTs(task.threadTs)})`);
      debugInfo.push(`  - Source: ${task.source}`);
      debugInfo.push(`  - Created: ${formatTimestamp(task.createdAt)}`);
      if (task.statusMessageTs) {
        debugInfo.push(`  - Status Message: \`${task.statusMessageTs}\``);
      }
    }
  }
  debugInfo.push('');

  // Thread context info
  debugInfo.push('*Thread Context:*');
  debugInfo.push("_Note: Slash commands don't receive thread context directly._");
  debugInfo.push('_To check a specific thread, provide the thread timestamp:_');
  debugInfo.push(`\`${command.command} debug <thread_ts>\``);
  debugInfo.push('');

  // Check if user provided a thread_ts as argument
  if (args) {
    const threadTs = args.trim();
    const contextKey = contextStore.buildKey(teamId, channelId, threadTs);
    const contextRecord = await contextStore.get(contextKey);

    debugInfo.push(`*Thread Lookup (${threadTs}):*`);
    debugInfo.push(`• Context Key: \`${contextKey}\``);
    if (contextRecord) {
      debugInfo.push(`• Context ID: \`${contextRecord.contextId}\``);
      if (contextRecord.lastProcessedTs) {
        debugInfo.push(`• Last Processed: ${formatSlackTs(contextRecord.lastProcessedTs)}`);
      }
      debugInfo.push(`• Thread Time: ${formatSlackTs(threadTs)}`);
    } else {
      debugInfo.push('• No context found for this thread');
    }

    // Check if there's an in-flight task for this specific thread
    const threadTasks = inFlightTasks.filter((t) => t.threadTs === threadTs);
    if (threadTasks.length > 0) {
      debugInfo.push(`• In-flight tasks in this thread: ${threadTasks.length}`);
    }
  }

  await respond({
    response_type: 'ephemeral',
    text: debugInfo.join('\n'),
    mrkdwn: true,
  });
}

/**
 * Show help for the bot command
 */
async function handleHelpSubcommand({ command, respond }: NannosCommand, botName: string): Promise<void> {
  const cmd = command.command;
  const helpText = [
    `🤖 *${botName} Commands*\n`,
    `\`${cmd} login\` - Log in to use ${botName} services`,
    `\`${cmd} debug [thread_ts]\` - Show debug info about your session and threads`,
    `\`${cmd} help\` - Show this help message`,
  ].join('\n');

  await respond({
    response_type: 'ephemeral',
    text: helpText,
    mrkdwn: true,
  });
}

/**
 * Handle bot slash command with subcommands
 */
async function handleNannosCommand(
  args: NannosCommand,
  userAuthService: UserAuthService,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore,
  pendingRequestStore: IPendingRequestStore,
  botName: string
): Promise<void> {
  const logger = Logger.getLogger('handleNannosCommand');
  const { command, ack, respond } = args;

  try {
    await ack();

    // Parse subcommand and arguments
    const text = command.text?.trim() || '';
    const [subcommand, ...subArgs] = text.split(/\s+/);
    const subArgsText = subArgs.join(' ');

    logger.info(`${command.command} ${subcommand} from user ${command.user_id}`);

    switch (subcommand.toLowerCase()) {
      case 'login':
        await handleLoginSubcommand(args, userAuthService, botName);
        break;

      case 'debug':
        await handleDebugSubcommand(
          args,
          userAuthService,
          contextStore,
          inFlightTaskStore,
          pendingRequestStore,
          subArgsText,
          botName
        );
        break;

      case 'help':
      case '':
        await handleHelpSubcommand(args, botName);
        break;

      default:
        await respond({
          response_type: 'ephemeral',
          text: `❓ Unknown subcommand: \`${subcommand}\`\n\nUse \`${command.command} help\` to see available commands.`,
        });
    }
  } catch (error) {
    logger.error(error, `Error handling ${command.command} command: ${error}`);
    await respond({
      response_type: 'ephemeral',
      text: `❌ An error occurred: ${error}`,
    }).catch(() => {});
  }
}

/**
 * Register a single slash command handler.
 *
 * The slash command string (e.g. "/nannos" or "/mybot") and the services are
 * passed in directly.  botName is resolved per-invocation from Bolt's
 * authorize context so each bot persona is handled correctly.
 */
export function registerNannosCommand(
  app: App,
  slashCommand: string,
  userAuthService: UserAuthService,
  contextStore: IContextStore,
  inFlightTaskStore: IInFlightTaskStore,
  pendingRequestStore: IPendingRequestStore,
  _oauthStateStore: IOAuthStateStore
): void {
  const logger = Logger.getLogger('registerNannosCommand');
  logger.info(`Registering ${slashCommand} command`);

  app.command(slashCommand, async (args) => {
    const botName = ((args.context as any).botName as string | undefined) ?? 'Bot';
    await handleNannosCommand(args, userAuthService, contextStore, inFlightTaskStore, pendingRequestStore, botName);
  });
}
