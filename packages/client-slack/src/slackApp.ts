import { App, LogLevel } from '@slack/bolt';
import { WebClient } from '@slack/web-api';
import { Config } from './config/config.js';
import { Logger, SlackBoltLogger } from './utils/logger.js';
import { createStorageProvider, type StorageProvider } from './storage/index.js';
import { OIDCClient } from './services/oidcClient.js';
import { UserAuthService } from './services/userAuthService.js';
import { A2AClientService } from './services/a2aClientService.js';
import { FileStorageService } from './services/fileStorageService.js';
import { FeedbackService } from './services/feedbackService.js';
import { registerListeners } from './listeners/index.js';
import { handleOAuthCallback, generateCallbackHTML } from './utils/oauthCallback.js';
import { processPendingRequest } from './utils/processPendingRequest.js';
import { recoverOrphanedTasks } from './utils/taskRecovery.js';
import { MultiTenantHTTPReceiver } from './receivers/MultiTenantHTTPReceiver.js';
import { ParamsIncomingMessage } from '@slack/bolt/dist/receivers/ParamsIncomingMessage.js';
import { ServerResponse } from 'node:http';
let userAuthService: UserAuthService;
let a2aClientService: A2AClientService;
let fileStorageService: FileStorageService;
let feedbackService: FeedbackService | undefined;
let storage: StorageProvider;

// Initialize logger early

// Slack events endpoint prefix
const SLACK_EVENTS_PATH = '/api/v1/slack/events';

export async function startSlackApp(config: Config) {
  const logger = Logger.getLogger('ApiV1');
  try {
    // Update app configuration with loaded config
    const socketMode = config.slackAppConfig.socketMode;

    // Storage layer (PostgreSQL) — must be initialised before the Bolt App so that
    // the authorize callback and startup seed can use it.
    storage = await createStorageProvider(config.storage);

    // -------------------------------------------------------------------------
    // Zero-downtime migration: upsert the initial Nannos bot installation.
    // If SLACK_BOT_TOKEN + SLACK_APP_ID + SLACK_TEAM_ID are all present we insert
    // (or update credentials on conflict) so the existing bot keeps working after
    // the first deploy without any manual admin steps.
    // bot_name and slash_command are NOT overwritten on conflict so that admin edits
    // are preserved across re-deploys.
    // -------------------------------------------------------------------------
    const {
      seedAppId,
      seedTeamId,
      botToken: seedBotToken,
      signingSecret,
      seedBotName,
      seedSlashCommand,
    } = config.slackAppConfig;
    if (seedAppId && seedTeamId && seedBotToken) {
      logger.info(`Upserting seed bot installation for appId=${seedAppId} (${seedBotName ?? 'Nannos'})...`);
      await storage.botInstallation.upsert({
        appId: seedAppId,
        teamId: seedTeamId,
        botToken: seedBotToken,
        signingSecret: signingSecret,
        botName: seedBotName ?? 'Nannos',
        slashCommand: seedSlashCommand ?? '/nannos',
        isActive: true,
      });
      logger.info(`Seed bot installation upserted successfully`);
    } else {
      logger.info(
        'Seed env vars (SLACK_APP_ID / SLACK_TEAM_ID / SLACK_BOT_TOKEN) not fully set — skipping bot installation seed'
      );
    }

    // ----- Custom routes (used by both HTTP receiver and socket-mode) -----
    const customRoutes = [
      {
        path: '/api/v1/health',
        method: 'GET' as const,
        handler: (_req: ParamsIncomingMessage, res: ServerResponse) => {
          res.end('OK');
        },
      },
      {
        path: '/api/v1/authorize',
        method: 'GET' as const,
        handler: async (req: ParamsIncomingMessage, res: ServerResponse) => {
          try {
            const url = new URL(req.url!, config.baseUrl);
            const state = url.searchParams.get('state');

            if (!state) {
              res.writeHead(400, { 'Content-Type': 'text/plain' });
              res.end('Missing state parameter');
              return;
            }

            const stateEntry = await storage.oauthState.get(state);

            if (!stateEntry || stateEntry.expiresAt < Date.now()) {
              res.writeHead(400, { 'Content-Type': 'text/plain' });
              res.end('Invalid or expired state');
              return;
            }

            const { userId, teamId, codeVerifier } = stateEntry;
            const authUrl = await userAuthService.getAuthorizationUrl(state, teamId, codeVerifier);

            logger.info(`Redirecting user ${userId} to OIDC authorization URL`);
            res.writeHead(302, { Location: authUrl });
            res.end();
          } catch (error) {
            logger.error(error, `Authorization redirect error: ${error}`);
            res.writeHead(500, { 'Content-Type': 'text/plain' });
            res.end('An error occurred during authorization');
          }
        },
      },
      {
        path: '/api/v1/oauth/callback',
        method: 'GET' as const,
        handler: async (req: ParamsIncomingMessage, res: ServerResponse) => {
          try {
            const url = new URL(req.url!, config.baseUrl);
            const queryParams = url.searchParams;
            const baseUrl = new URL(url.pathname, config.baseUrl).toString();

            const result = await handleOAuthCallback(queryParams, userAuthService, baseUrl, storage.oauthState);

            res.writeHead(200, { 'Content-Type': 'text/html' });
            res.end(generateCallbackHTML(result.success, result.message));

            if (result.success && result.userId && result.teamId) {
              const pendingRequest = await storage.pendingRequest.consume(result.teamId, result.userId);

              if (pendingRequest) {
                logger.info(`Found pending request for user ${result.userId}, processing...`);

                const pendingBot = pendingRequest.appId
                  ? await storage.botInstallation.getByAppId(pendingRequest.appId)
                  : (await storage.botInstallation.getByTeamId(result.teamId))[0];
                const pendingBotToken = pendingBot?.botToken ?? config.slackAppConfig.botToken;
                const pendingBotName = pendingBot?.botName ?? 'Nannos';

                processPendingRequest(pendingRequest, new WebClient(pendingBotToken), {
                  userAuthService,
                  a2aClientService,
                  contextStore: storage.context,
                  pendingRequestStore: storage.pendingRequest,
                  inFlightTaskStore: storage.inFlightTask,
                  baseUrl: config.baseUrl,
                  botToken: pendingBotToken!,
                  botName: pendingBotName,
                  fileStorageService,
                  isLocalMode: config.isLocal(),
                }).catch((error) => {
                  logger.error(error, `Failed to process pending request: ${error}`);
                });
              } else {
                try {
                  const notifyBot = (await storage.botInstallation.getByTeamId(result.teamId))[0];
                  const notifyToken =
                    notifyBot?.botToken ?? config.slackAppConfig.botToken ?? config.slackAppConfig.appToken;

                  const dmResult = await app.client.conversations.open({
                    token: notifyToken,
                    users: result.userId,
                  });

                  if (dmResult.ok && dmResult.channel?.id) {
                    await app.client.chat.postMessage({
                      token: notifyToken,
                      channel: dmResult.channel.id,
                      text: '✅ Authorization successful! You can now start to use the Slack bot.',
                    });
                  }
                } catch (error) {
                  logger.error(`Failed to send DM notification: ${error}`);
                }
              }
            }
          } catch (error) {
            logger.error(error, `OAuth callback error: ${error}`);
            res.writeHead(500, { 'Content-Type': 'text/html' });
            res.end(generateCallbackHTML(false, 'An unexpected error occurred.'));
          }
        },
      },
      {
        path: '/api/v1/a2a/callback',
        method: 'POST' as const,
        handler: async (_req: ParamsIncomingMessage, _res: ServerResponse) => {
          logger.warn('Received request on /api/v1/a2a/callback — NOT IMPLEMENTED YET');
        },
      },
    ];

    // ----- Build the Bolt App -----
    // In HTTP mode, use the custom MultiTenantHTTPReceiver for per-app signing
    // secret verification. In socket mode, use Bolt's built-in SocketModeReceiver.
    const receiver = !socketMode
      ? new MultiTenantHTTPReceiver({
          botInstallationStore: storage.botInstallation,
          endpoints: SLACK_EVENTS_PATH,
          port: config.slackAppPort,
          customRoutes,
        })
      : undefined;

    const app = new App({
      logger: new SlackBoltLogger('SlackApp'),
      port: config.slackAppPort,
      authorize: async (_source, body) => {
        const appId = (body as any)?.api_app_id as string | undefined;
        if (!appId) {
          if (socketMode && seedBotToken) {
            return { botToken: seedBotToken, botName: seedBotName ?? 'Nannos' };
          }
          throw new Error('authorize: api_app_id not found in request body');
        }
        const bot = await storage.botInstallation.getByAppId(appId);
        if (!bot) {
          throw new Error(`authorize: no active bot installation found for appId=${appId}`);
        }
        return { botToken: bot.botToken, botName: bot.botName };
      },
      // HTTP mode: use custom receiver with per-app signing secret verification
      ...(receiver && { receiver }),
      // Socket mode: use built-in SocketModeReceiver
      ...(socketMode && {
        appToken: config.slackAppConfig.appToken,
        signingSecret: config.slackAppConfig.signingSecret,
        socketMode: true,
        customRoutes,
      }),
      // HTTP mode without custom receiver would need these, but we always use the custom receiver
      logLevel: LogLevel.INFO,
    });

    // Set log level
    logger.setLevel(config.logLevel);

    // Initialize services
    logger.info('Initializing services...');

    // OIDC client
    const oidcClient = new OIDCClient(config);

    // User auth service (assign to module-level variable for OAuth callback)
    userAuthService = new UserAuthService(storage.userAuth, oidcClient, config, storage.oauthState);

    // A2A client service (assign to module-level variable for pending request processing)
    a2aClientService = new A2AClientService(config.a2aServer.url, config.a2aServer.timeout);

    // File storage service for S3 uploads
    fileStorageService = new FileStorageService(config);

    // Feedback service for console-backend integration (optional)
    if (config.consoleBackend) {
      feedbackService = new FeedbackService(userAuthService, config);
      logger.info(`Feedback service enabled (console-backend: ${config.consoleBackend.url})`);
    }

    // Log every incoming Slack event for observability
    app.use(async ({ body, next }) => {
      const eventType = (body as any).event?.type || (body as any).command || (body as any).type || 'unknown';
      const eventSubtype = (body as any).event?.subtype ? `:${(body as any).event.subtype}` : '';
      const eventUser = (body as any).event?.user || (body as any).user_id || 'unknown';
      const eventChannel = (body as any).event?.channel || (body as any).channel_id || 'unknown';
      logger.info(`[SlackEvent] type=${eventType}${eventSubtype} user=${eventUser} channel=${eventChannel}`);
      await next();
    });

    // Register listeners with services (botToken resolved per-event from authorize context)
    await registerListeners(
      app,
      userAuthService,
      a2aClientService,
      storage.context,
      storage.pendingRequest,
      storage.inFlightTask,
      storage.oauthState,
      config.baseUrl,
      fileStorageService,
      config.isLocal(),
      storage.botInstallation,
      feedbackService,
    );

    // Start the app
    const port = config.slackAppPort;
    await app.start(port);

    logger.info(`A2A Slack Client  is running on port ${port} in ${config.environment} mode`);
    logger.info(`Socket Mode: ${config.slackAppConfig.socketMode ? 'enabled' : 'disabled (HTTP mode)'}`);
    logger.info(`Storage Provider: postgres`);
    if (!config.slackAppConfig.socketMode) {
      logger.info(`Slack Events Endpoint: ${SLACK_EVENTS_PATH}`);
    }
    logger.info(`A2A Server: ${config.a2aServer.url}`);
    logger.info(`OIDC Issuer: ${config.oidc.issuerUrl}`);

    // Task recovery configuration
    const RECOVERY_MIN_AGE_MS = 2 * 60 * 1000; // 2 minutes - only recover tasks older than this
    const RECOVERY_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes - how often to run recovery

    // Helper function to run recovery
    // NOTE: This is not perfect, as if the app is scaled horizontally, multiple instances may run recovery simultaneously.
    // This is a known limitation and acceptable for now during development phase.
    const runRecovery = async () => {
      try {
        await recoverOrphanedTasks(
          storage.inFlightTask,
          a2aClientService,
          userAuthService,
          app.client,
          storage.context,
          RECOVERY_MIN_AGE_MS
        );
      } catch (error) {
        logger.error(error, `Task recovery failed: ${error}`);
      }
    };

    // Run recovery immediately on startup (don't block)
    runRecovery().catch((error) => {
      logger.error(error, `Initial task recovery failed: ${error}`);
    });

    // Run recovery periodically to catch any orphaned tasks
    const recoveryInterval = setInterval(runRecovery, RECOVERY_INTERVAL_MS);
    logger.info(`Task recovery scheduled every ${RECOVERY_INTERVAL_MS / 1000 / 60} minutes`);

    // Clean up on process exit
    const cleanup = async () => {
      logger.info('Shutting down...');
      clearInterval(recoveryInterval);

      try {
        // Shutdown storage provider (close connections, etc.)
        await storage.shutdown();
        logger.info('Storage provider shutdown successfully');
      } catch (error) {
        logger.error(error, `Error shutting down storage: ${error}`);
      }

      try {
        // Stop the Bolt app gracefully
        await app.stop();
        logger.info('App stopped successfully');
      } catch (error) {
        logger.error(error, `Error stopping app: ${error}`);
      }

      process.exit(0);
    };

    process.on('SIGTERM', cleanup);
    process.on('SIGINT', cleanup);

    return app;
  } catch (error) {
    const err = error as Error;
    logger.error(
      error,
      `Error occurred when starting the A2A Slack Client . Error: ${err.message || JSON.stringify(err)}`
    );
    process.exit(1);
  }
}
