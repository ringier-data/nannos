import { App } from '@slack/bolt';
import { UserAuthService } from '../services/userAuthService.js';
import { A2AClientService } from '../services/a2aClientService.js';
import { FileStorageService } from '../services/fileStorageService.js';
import { FeedbackService } from '../services/feedbackService.js';
import type {
  IContextStore,
  IPendingRequestStore,
  IInFlightTaskStore,
  IOAuthStateStore,
  IBotInstallationStore,
} from '../storage/types.js';
import { registerAppMentionListener } from './events/appMention.js';
import { registerMessageListeners } from './events/directMessage.js';
import { registerNannosCommand } from './commands/nannos.js';
import { registerAuthorizeButtonAction } from './actions/authorizeButton.js';
import { registerFeedbackButtonActions } from './actions/feedbackButton.js';
import { registerBugReportActions } from './actions/bugReportButton.js';
import { registerBugReportModalHandler } from './views/bugReportModal.js';
import { registerReactionListeners } from './events/reactionHandler.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('registerListeners');

/**
 * Register all event listeners, commands, and actions with the Slack app.
 *
 * botToken is no longer a static parameter — it is resolved per-event from
 * Bolt's authorize callback.  Slash commands are registered dynamically for
 * every active bot installation found in the database.
 */
export async function registerListeners(
  app: App,
  userAuthService: UserAuthService,
  a2aClientService: A2AClientService,
  contextStore: IContextStore,
  pendingRequestStore: IPendingRequestStore,
  inFlightTaskStore: IInFlightTaskStore,
  oauthStateStore: IOAuthStateStore,
  baseUrl: string,
  fileStorageService: FileStorageService,
  isLocalMode: boolean,
  botInstallationStore: IBotInstallationStore,
  feedbackService?: FeedbackService,
): Promise<void> {
  // Register event listeners (botToken/botName resolved per-event via context)
  registerAppMentionListener(
    app,
    userAuthService,
    a2aClientService,
    contextStore,
    pendingRequestStore,
    inFlightTaskStore,
    oauthStateStore,
    baseUrl,
    fileStorageService,
    isLocalMode,
    feedbackService,
  );
  registerMessageListeners(
    app,
    userAuthService,
    a2aClientService,
    contextStore,
    pendingRequestStore,
    inFlightTaskStore,
    oauthStateStore,
    baseUrl,
    fileStorageService,
    isLocalMode,
    feedbackService,
  );

  // Register slash commands dynamically for every active bot installation
  const bots = await botInstallationStore.listAll();
  const registeredCommands = new Set<string>();

  for (const bot of bots) {
    if (!bot.slashCommand || registeredCommands.has(bot.slashCommand)) continue;
    registeredCommands.add(bot.slashCommand);
    registerNannosCommand(
      app,
      bot.slashCommand,
      userAuthService,
      contextStore,
      inFlightTaskStore,
      pendingRequestStore,
      oauthStateStore
    );
  }

  logger.info(`Registered slash commands: ${[...registeredCommands].join(', ') || '(none)'}`);

  // Register actions
  registerAuthorizeButtonAction(app);

  // Register feedback button actions (requires console-backend)
  if (feedbackService) {
    registerFeedbackButtonActions(app, feedbackService);
  }

  // Register bug report button and modal handlers
  // A factory is used because botToken/botName are normally resolved per-event,
  // but bug report interactions (view submissions, button clicks) don't have
  // the file-download context, so an empty botToken is fine.
  const makeBugReportDeps = () => ({
    userAuthService,
    a2aClientService,
    contextStore,
    pendingRequestStore,
    inFlightTaskStore,
    baseUrl,
    botToken: '',
    botName: '',
    fileStorageService,
    isLocalMode,
    feedbackService,
  });
  registerBugReportActions(app, makeBugReportDeps);
  registerBugReportModalHandler(app, makeBugReportDeps);

  // Register reaction listeners for message feedback (requires console-backend)
  if (feedbackService) {
    registerReactionListeners(app, feedbackService);
  }
}
