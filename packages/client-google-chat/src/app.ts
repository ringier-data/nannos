import express, { Request, Response } from 'express';
import { Server } from 'http';
import { Config, getConfigFromEnv } from './config/config.js';
import { Logger } from './utils/logger.js';
import { createStorageProvider, type StorageProvider } from './storage/index.js';
import { OIDCClient } from './services/oidcClient.js';
import { UserAuthService } from './services/userAuthService.js';
import { A2AClientService } from './services/a2aClientService.js';
import { FileStorageService } from './services/fileStorageService.js';
import { GoogleChatService } from './services/googleChatService.js';
import { FeedbackService } from './services/feedbackService.js';
import { createGoogleChatAuthMiddleware } from './middleware/googleChatAuth.js';
import { handleOAuthCallback, generateCallbackHTML } from './utils/oauthCallback.js';
import { processPendingRequest } from './utils/processPendingRequest.js';
import { recoverOrphanedTasks } from './utils/taskRecovery.js';
import { handleIncomingMessage, type NormalizedMessage } from './handlers/messageHandler.js';
import type { GoogleChatAttachment } from './utils/fileUtils.js';
import { HandlerDependencies } from './handlers/types.js';
import { AppCommand, handleAppCommand } from './handlers/commandHandler.js';
import { ButtonClickedPayload, handleButtonClicked } from './handlers/buttonClickedHandler.js';

// Initialize logger early
const logger = Logger.getLogger('app');

process.setMaxListeners(20);

// ---------------------------------------------------------------------------
// Google Chat event types
// ---------------------------------------------------------------------------

// https://developers.google.com/workspace/add-ons/concepts/event-objects#chat-event-object
interface User {
  name: string; // e.g. "users/123456789"
  email: string;
  displayName?: string;
}

interface Space {
  name: string; // e.g. "spaces/AAAAB3NzaC1yc2EAAAADAQABAAABAQC..."
  spaceType: 'DIRECT_MESSAGE' | 'GROUP_CHAT' | 'SPACE';
}

interface BaseGoogleChatEvent {
  chat: {
    user: User;
    messagePayload?: any;
    appCommandPayload?: any;
    buttonClickedPayload?: any;
  };
}

interface MessageGoogleChatEvent {
  chat: {
    user: User;
    messagePayload: {
      space: Space;
      message: {
        name: string;
        argumentText: string;
        thread?: {
          name: string;
        };
        attachment?: Array<GoogleChatAttachment>;
      };
    };
  };
}

interface AppCommandGoogleChatEvent {
  chat: {
    user: User;
    appCommandPayload: {
      space: Space;
      message: {
        name: string;
        text: string;
        argumentText: string;
        thread?: {
          name: string;
        };
      };
    };
  };
}

interface ButtonClickedGoogleChatEvent {
  commonEventObject: {
    parameters: {cardId: string; action: string, parameters: string};
  }
  chat: {
    user: User;
    buttonClickedPayload: {
      space: Space;
      message: {
        name: string;
        text: string;
        argumentText: string;
        thread?: {
          name: string;
        };
      };
    };
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function setupServerTimeouts(server: Server, config: Config) {
  // Ensure all inactive connections are terminated by the ALB, by setting this a few seconds higher than the ALB idle timeout
  server.keepAliveTimeout = config.httpKeepAliveTimeout;
  // Ensure the headersTimeout is set higher than the keepAliveTimeout due to this nodejs regression bug: https://github.com/nodejs/node/issues/27363
  server.headersTimeout = config.httpKeepAliveTimeout + 2e3;
  // Ensure TCP timeout does not happen before
  server.timeout = config.httpKeepAliveTimeout + 4e3;
}

(async () => {
  try {
    // Load configuration
    const config = await getConfigFromEnv();
    if (config.isLocal()) {
      process.on('SIGINT', () => {
        process.exit(0);
      });
    }

    // Set log level
    logger.setLevel(config.logLevel as any);
    logger.setName('a2a-google-chat-client');

    const app = express();

    // JSON body parser
    app.use(express.json());

    // Initialize services
    logger.info('Initializing services...');

    // Storage layer
    const storage: StorageProvider = await createStorageProvider(config.storage);

    // OIDC client
    const oidcClient = new OIDCClient(config);

    // User auth service
    const userAuthService = new UserAuthService(storage.userAuth, oidcClient, config, storage.oauthState);

    // A2A client service
    const a2aClientService = new A2AClientService(config.a2aServer.url, config.a2aServer.timeout);

    // File storage service for S3 uploads
    const fileStorageService = new FileStorageService(config);

    // Google Chat service (uses service account credentials)
    const chatService = new GoogleChatService(config);

    // Feedback service for console-backend integration (optional)
    let feedbackService: FeedbackService | undefined;
    if (config.consoleBackend) {
      feedbackService = new FeedbackService(userAuthService, config);
      logger.info(`Feedback service enabled (console-backend: ${config.consoleBackend.url})`);
    }

    // Handler dependencies
    const handlerDeps: HandlerDependencies = {
      userAuthService,
      a2aClientService,
      chatService,
      contextStore: storage.context,
      pendingRequestStore: storage.pendingRequest,
      inFlightTaskStore: storage.inFlightTask,
      fileStorageService,
      feedbackService,
      config,
    };

    // -----------------------------------------------------------------------
    // Routes
    // -----------------------------------------------------------------------

    // Health check
    app.get('/api/v1/health', (_req: Request, res: Response) => {
      res.send('OK');
    });

    // Google Chat event endpoint (with JWT verification)
    app.post(
      '/api/v1/chat/events',
      createGoogleChatAuthMiddleware(
        config.googleChatTokenExpectedAudience,
        config.googleChatConfigs,
      ),
      async (req: Request, res: Response) => {
        // Log raw event structure for debugging card clicks
        const topLevelKeys = Object.keys(req.body || {});
        const chatKeys = req.body?.chat ? Object.keys(req.body.chat) : [];
        logger.info(`[ChatEvent] Raw event keys: top=${topLevelKeys.join(',')}, chat=${chatKeys.join(',')}`);

        const baseEvent: BaseGoogleChatEvent = req.body;

        if (!baseEvent?.chat?.user) {
          logger.warn(`[ChatEvent] Received event with unexpected structure (no chat.user). Keys: ${topLevelKeys.join(',')}`);
          res.json({});
          return;
        }

        const projectId = res.locals.projectNumber;

        const userId = baseEvent.chat.user.name;
        const userEmail = baseEvent.chat.user.email;
        let eventType: string | undefined;
        if (baseEvent.chat?.messagePayload) {
          eventType = 'MESSAGE';
        } else if (baseEvent.chat?.appCommandPayload) {
          eventType = 'APP_COMMAND';
        } else if (baseEvent.chat?.buttonClickedPayload) {
          eventType = 'BUTTON_CLICKED';
        }

        logger.info(`[ChatEvent] type=${eventType} user=${userId}`);

        try {
          switch (eventType) {
            case 'MESSAGE': {
              const event = baseEvent as MessageGoogleChatEvent;
              const spaceId = event.chat.messagePayload.space.name;
              const messageText = event.chat.messagePayload.message.argumentText?.trim() || '';
              const messageId = event.chat.messagePayload.message.name || '';
              const threadId = event.chat.messagePayload.message.thread?.name || messageId;
              const isDm = event.chat.messagePayload.space.spaceType === 'DIRECT_MESSAGE';
              const source = isDm ? 'direct_message' : 'space_message';

              // Map Google Chat attachments
              let attachments: GoogleChatAttachment[] | undefined;
              if (
                event.chat.messagePayload?.message?.attachment &&
                event.chat.messagePayload.message.attachment.length > 0
              ) {
                attachments = event.chat.messagePayload.message.attachment
                  .filter((a) => a.source === 'UPLOADED_CONTENT' && a.attachmentDataRef?.resourceName)
                  .map((a) => ({
                    name: a.name,
                    contentName: a.contentName,
                    contentType: a.contentType,
                    attachmentDataRef: a.attachmentDataRef,
                    source: a.source,
                  }));
              }

              const normalizedMsg: NormalizedMessage = {
                userId,
                userEmail,
                projectId,
                spaceId,
                messageId: messageId,
                threadId: threadId,
                rawText: messageText,
                attachments,
                source,
              };

              // Process asynchronously so we respond to Google Chat quickly
              handleIncomingMessage(normalizedMsg, handlerDeps).catch((error) => {
                logger.error(error, `Error handling MESSAGE event: ${error}`);
              });

              // Respond immediately to acknowledge the event
              res.json({});
              return;
            }

            case 'APP_COMMAND': {
              const event = baseEvent as AppCommandGoogleChatEvent;
              const commandArgument = event.chat.appCommandPayload.message.argumentText?.trim() || '';
              const spaceId = event.chat.appCommandPayload.space.name;
              const messageId = event.chat.appCommandPayload.message.name || '';
              const threadId = event.chat.appCommandPayload.message.thread?.name || messageId;

              const appCommand: AppCommand = {
                commandArgument,
                spaceId,
                userId,
                projectId,
                threadId,
                messageId,
              };

              // Process asynchronously so we respond to Google Chat quickly
              handleAppCommand(appCommand, handlerDeps).catch((error) => {
                logger.error(error, `Error handling APP_COMMAND event: ${error}`);
              });

              // Respond immediately to acknowledge the event
              res.json({});
              return;
            }

            case 'BUTTON_CLICKED': {
              const event = baseEvent as ButtonClickedGoogleChatEvent;

              const messageId = event.chat.buttonClickedPayload.message.name || '';
              const threadId = event.chat.buttonClickedPayload.message.thread?.name || messageId;

              const buttonPayload: ButtonClickedPayload = {
                cardId: event.commonEventObject.parameters.cardId,
                action: event.commonEventObject.parameters.action,
                actionParameters: JSON.parse(event.commonEventObject.parameters.parameters),
                userId,
                userEmail,
                projectId,
                spaceId: event.chat.buttonClickedPayload.space.name,
                threadId,
                messageId,
              };

              handleButtonClicked(buttonPayload, handlerDeps).catch((error) => {
                logger.error(error, `Error handling BUTTON_CLICKED event: ${error}`);
              });

              res.json({});
              return;
            }

            default:
              logger.debug(`Unhandled event type: ${eventType}`);
              res.json({});
              return;
          }
        } catch (error) {
          logger.error(error, `Error handling chat event: ${error}`);
          res.json({});
        }
      }
    );

    // OAuth authorize endpoint (redirect to OIDC)
    app.get('/api/v1/authorize', async (req: Request, res: Response) => {
      try {
        const state = req.query.state as string;

        if (!state) {
          res.status(400).type('text/plain').send('Missing state parameter');
          return;
        }

        // Retrieve user info from state
        const stateEntry = await storage.oauthState.get(state);

        if (!stateEntry || stateEntry.expiresAt < Date.now()) {
          res.status(400).type('text/plain').send('Invalid or expired state');
          return;
        }

        const { userId, projectId, codeVerifier } = stateEntry;

        // Generate OIDC authorization URL
        const authUrl = await userAuthService.getAuthorizationUrl(state, projectId, codeVerifier);

        // Redirect to OIDC issuer
        logger.info(`Redirecting user ${userId} to OIDC authorization URL`);
        res.redirect(302, authUrl);
      } catch (error) {
        logger.error(error, `Authorization redirect error: ${error}`);
        res.status(500).type('text/plain').send('An error occurred during authorization');
      }
    });

    // OAuth callback endpoint
    app.get('/api/v1/oauth/callback', async (req: Request, res: Response) => {
      try {
        const url = new URL(req.url!, config.baseUrl);
        const queryParams = url.searchParams;

        // Get the base callback URL (without query parameters)
        const baseUrl = new URL(url.pathname, config.baseUrl).toString();

        // Handle OAuth callback
        const result = await handleOAuthCallback(queryParams, userAuthService, baseUrl, storage.oauthState);

        // Return HTML response immediately
        res.status(200).type('text/html').send(generateCallbackHTML(result.success, result.message));

        // Process pending request asynchronously
        if (result.success && result.userId && result.projectId) {
          const pendingRequest = await storage.pendingRequest.consume(result.projectId, result.userId);

          if (pendingRequest) {
            logger.info(`Found pending request for user ${result.userId}, processing...`);

            processPendingRequest(pendingRequest, chatService, handlerDeps).catch((error) => {
              logger.error(error, `Failed to process pending request: ${error}`);
            });
          } else {
            // No pending request — try to send a DM notification via Google Chat
            try {
              const dmSpace = await chatService.findDirectMessage(result.projectId, result.userId);
              if (dmSpace?.name) {
                await chatService.sendTextMessage(
                  result.projectId,
                  dmSpace.name,
                  '✅ Authorization successful! You can now send me messages.'
                );
                logger.info(`Sent DM confirmation to user ${result.userId} in space ${dmSpace.name}`);
              } else {
                logger.info(`Authorization successful for user ${result.userId}, no DM space found`);
              }
            } catch (error) {
              logger.debug(`No DM notification sent: ${error}`);
            }
          }
        }
      } catch (error) {
        logger.error(error, `OAuth callback error: ${error}`);
        res.status(500).type('text/html').send(generateCallbackHTML(false, 'An unexpected error occurred.'));
      }
    });

    // A2A webhook callback endpoint
    app.post('/api/v1/a2a/callback', async (_req: Request, _res: Response) => {
      logger.warn('Received request on /api/v1/a2a/callback — NOT IMPLEMENTED YET');
    });

    // -----------------------------------------------------------------------
    // Start server
    // -----------------------------------------------------------------------

    const port = config.appPort;

    const server = app.listen(port, () => {
      logger.info(`A2A Google Chat Client is running on port ${port} in ${config.environment} mode`);
      logger.info(`Storage Provider: ${config.storage.provider}`);
      logger.info(`A2A Server: ${config.a2aServer.url}`);
      logger.info(`OIDC Issuer: ${config.oidc.issuerUrl}`);
    });
    setupServerTimeouts(server, config);

    // -----------------------------------------------------------------------
    // Task recovery
    // -----------------------------------------------------------------------

    const RECOVERY_MIN_AGE_MS = 2 * 60 * 1000; // 2 minutes
    const RECOVERY_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes

    const runRecovery = async () => {
      try {
        await recoverOrphanedTasks(
          storage.inFlightTask,
          a2aClientService,
          userAuthService,
          chatService,
          storage.context,
          RECOVERY_MIN_AGE_MS
        );
      } catch (error) {
        logger.error(error, `Task recovery failed: ${error}`);
      }
    };

    // Run recovery immediately on startup
    runRecovery().catch((error) => {
      logger.error(error, `Initial task recovery failed: ${error}`);
    });

    // Run recovery periodically
    const recoveryInterval = setInterval(runRecovery, RECOVERY_INTERVAL_MS);
    logger.info(`Task recovery scheduled every ${RECOVERY_INTERVAL_MS / 1000 / 60} minutes`);

    // -----------------------------------------------------------------------
    // Graceful shutdown
    // -----------------------------------------------------------------------
    const cleanup = async () => {
      logger.info('Shutting down...');
      clearInterval(recoveryInterval);

      try {
        await storage.shutdown();
        logger.info('Storage provider shutdown successfully');
      } catch (error) {
        logger.error(error, `Error shutting down storage: ${error}`);
      }

      server.close(() => {
        logger.info('HTTP server closed');
        process.exit(0);
      });

      // Force exit after 10 seconds
      setTimeout(() => {
        logger.warn('Forced shutdown after timeout');
        process.exit(1);
      }, 10000);
    };

    process.on('SIGTERM', cleanup);
    process.on('SIGINT', cleanup);

    const handleError = (err: Error) => {
      try {
        logger.error({ err }, 'Unhandled exception/rejection occurred: ' + err.message);
      } catch (errInner) {
        // eslint-disable-next-line no-console
        console.error('Error occurred while logging unhandled exception/rejection');
        // eslint-disable-next-line no-console
        console.error(errInner);
        // eslint-disable-next-line no-console
        console.error(err);
      }
    };
    process.on('uncaughtException', handleError);
    process.on('unhandledRejection', handleError);
  } catch (error) {
    const err = error as Error;
    logger.error(
      error,
      `Error occurred when starting the A2A Google Chat Client. Error: ${err.message || JSON.stringify(err)}`
    );
    process.exit(1);
  }
})();
