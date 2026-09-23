import express from 'express';
import { getConfigFromEnv } from './config/config.js';
import { Logger } from './utils/logger.js';
import { Storage } from './storage/storage.js';
import { OIDCClient } from './services/oidcClient.js';
import { createUserAuthService } from './services/userAuthServiceFactory.js';
import { A2AClientService } from './services/a2aClientService.js';
import { FileStorageService } from './services/fileStorageService.js';
import { EmailOutboundService } from './services/emailOutboundService.js';
import { EmailInboundService } from './services/emailInboundService.js';
import { handleA2AWebhook } from './utils/a2aWebhookHandler.js';
import { handleOAuthCallback, generateCallbackHTML } from './utils/oauthCallback.js';
import { recoverOrphanedTasks, recoverStuckPendingRequests, resetStuckProcessedEmails } from './utils/taskRecovery.js';
import { LocalSqsPoller } from './services/localSqsPoller.js';

const logger = Logger.getLogger('app');

async function main() {
  const config = await getConfigFromEnv();
  logger.info('Configuration loaded');

  // Initialize storage
  const storage = new Storage(config);

  // Initialize services
  const oidcClient = new OIDCClient(config);
  // USER_AUTH_MODE picks the client's own login or console-backend's token broker.
  const userAuthService = createUserAuthService(config, storage, oidcClient);
  const a2aClientService = new A2AClientService(config.a2aServer.url, config.a2aServer.timeout);
  const fileStorageService = new FileStorageService(config);
  const emailOutboundService = new EmailOutboundService(config);
  const emailInboundService = new EmailInboundService(
    config,
    storage,
    userAuthService,
    a2aClientService,
    fileStorageService,
    emailOutboundService
  );

  // Create Express app
  const app = express();

  // Parse raw body for SNS (needs raw text for signature verification)
  app.use('/api/v1/email/incoming', express.text({ type: '*/*', limit: '10mb' }));
  // Parse JSON for other routes
  app.use(express.json({ limit: '10mb' }));

  // --- Health check ---
  app.get('/api/v1/health', (_req, res) => {
    res.json({ status: 'ok', service: 'a2a-email-client' });
  });

  // --- Inbound email (SNS → SES) ---
  app.post('/api/v1/email/incoming', async (req, res) => {
    try {
      const result = await emailInboundService.handleSNSMessage(req.body);
      res.status(result.status).json({ message: result.message });
    } catch (err) {
      logger.error(err, 'Unhandled error in inbound email handler');
      res.status(500).json({ message: 'Internal server error' });
    }
  });

  // --- A2A webhook callback ---
  app.post('/api/v1/a2a/callback', async (req, res) => {
    try {
      const result = await handleA2AWebhook(req.body, storage, emailOutboundService);
      res.status(result.success ? 200 : 400).json(result);
    } catch (err) {
      logger.error(err, 'Unhandled error in A2A webhook handler');
      res.status(500).json({ success: false, message: 'Internal server error' });
    }
  });

  // --- OAuth authorize redirect ---
  app.get('/api/v1/authorize', async (req, res) => {
    try {
      const email = req.query.email as string;
      const stateParam = req.query.state as string;
      if (!email || !stateParam) {
        res.status(400).send('Missing email or state parameter');
        return;
      }

      // Look up stored state to get codeVerifier
      const stateData = await storage.consumeOAuthState(stateParam);
      if (!stateData) {
        res.status(400).send('Invalid or expired authorization link. Please send another email.');
        return;
      }

      // Generate auth URL and redirect
      const authUrl = await userAuthService.getAuthorizationUrl(stateParam, stateData.codeVerifier);

      // Re-store the state since consumeOAuthState deleted it and we still need it for the callback
      await userAuthService.storeAuthState(stateParam, email, stateData.codeVerifier);

      res.redirect(authUrl);
    } catch (err) {
      logger.error(err, 'Failed to generate authorization URL');
      res.status(500).send('Failed to start authorization. Please try again.');
    }
  });

  // --- OAuth callback ---
  app.get('/api/v1/oauth/callback', async (req, res) => {
    try {
      const queryParams = new URLSearchParams(req.url.split('?')[1] || '');
      const callbackBaseUrl = `${config.baseUrl}/api/v1/oauth/callback`;
      const result = await handleOAuthCallback(queryParams, userAuthService, callbackBaseUrl, storage);

      // If auth succeeded, process any pending request
      if (result.success && result.email) {
        // Fire-and-forget: process pending request in background
        processPendingRequestAfterAuth(result.email, emailInboundService, storage).catch((err) => {
          logger.error(err, `Failed to process pending request for ${result.email}`);
        });
      }

      res
        .status(result.success ? 200 : 400)
        .type('html')
        .send(generateCallbackHTML(result.success, result.message));
    } catch (err) {
      logger.error(err, 'Unhandled error in OAuth callback');
      res.status(500).type('html').send(generateCallbackHTML(false, 'An unexpected error occurred.'));
    }
  });

  // Start server
  const server = app.listen(config.appPort, () => {
    logger.info(`Email A2A client listening on port ${config.appPort}`);
    logger.info(`User sign-in: ${config.userAuthMode === 'broker' ? 'console-backend token broker' : 'local'}`);
  });

  // Ensure SNS subscription is active (idempotent)
  emailInboundService.ensureSnsSubscription().catch((err) => {
    logger.error(err, 'Failed to ensure SNS subscription on startup');
  });

  // Start local SQS poller for dev (only when ENVIRONMENT=local)
  let localSqsPoller: LocalSqsPoller | undefined;
  if (config.isLocal()) {
    localSqsPoller = new LocalSqsPoller(config, emailInboundService);
    localSqsPoller.start();
  }

  // --- Startup recovery ---
  // With streaming A2A communication, tasks complete synchronously within the
  // streaming loop. Recovery is only needed for crash scenarios:
  // 1. Orphaned tasks: app crashed mid-stream
  // 2. Stuck pending requests: app crashed after OAuth but before processing
  // 3. Stuck processed emails: app crashed mid-email-processing
  //
  // Run once on startup; periodic recovery is no longer needed.

  // Recover orphaned tasks (crash mid-stream)
  const recoveryMinAgeMs = config.isLocal() ? 5_000 : 2 * 60 * 1000;
  recoverOrphanedTasks(storage, a2aClientService, userAuthService, emailOutboundService, recoveryMinAgeMs).catch(
    (err) => {
      logger.error(err, 'Startup task recovery failed');
    }
  );

  // Recover pending requests stuck in 'processing' state (app crashed after claim, before delete)
  recoverStuckPendingRequests(storage, emailInboundService).catch((err) => {
    logger.error(err, 'Stuck pending request recovery failed');
  });

  // Reset processed_email records stuck in 'processing' (app crashed mid-processing)
  resetStuckProcessedEmails(storage).catch((err) => {
    logger.error(err, 'Reset stuck processed emails failed');
  });

  // Cleanup expired records periodically (context, inflight tasks)
  const cleanupIntervalMs = config.isLocal() ? 60 * 1000 : 10 * 60 * 1000;
  const cleanupInterval = setInterval(async () => {
    try {
      await storage.cleanupExpiredRecords();
    } catch (err) {
      logger.error(err, 'Cleanup expired records failed');
    }
  }, cleanupIntervalMs);

  // Graceful shutdown
  const shutdown = async (signal: string) => {
    logger.info(`Received ${signal}, shutting down...`);
    clearInterval(cleanupInterval);
    localSqsPoller?.stop();
    server.close(() => {
      logger.info('HTTP server closed');
      storage.shutdown().then(() => {
        logger.info('Storage closed');
        process.exit(0);
      });
    });

    // Force exit after 10 seconds
    setTimeout(() => {
      logger.warn('Forced shutdown after timeout');
      process.exit(1);
    }, 10_000);
  };

  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
}

/**
 * After successful OAuth, retrieve and process any pending request.
 * Uses claim-then-delete pattern: the row is marked 'processing' first,
 * and only deleted after successful processing. If the app crashes between
 * claim and delete, the row survives for startup recovery.
 */
async function processPendingRequestAfterAuth(
  email: string,
  emailInboundService: EmailInboundService,
  storage: Storage
): Promise<void> {
  const pending = await storage.claimPendingRequest(email);
  if (!pending) {
    logger.info(`No pending request found for ${email} after auth`);
    return;
  }

  logger.info(`Processing pending request for ${email}: subject="${pending.subject}"`);

  // Re-construct an InboundEmail from the pending request and run it through the inbound pipeline
  const inboundEmail = {
    senderEmail: email,
    subject: pending.subject || '',
    bodyText: pending.bodyText || '',
    messageId: pending.originalMessageId || '',
    attachments: [] as any[], // Attachment data is stored as S3 URIs in fileUris
  };

  try {
    await emailInboundService.processInboundEmail(inboundEmail);
    // Only delete after successful processing
    await storage.deletePendingRequest(email);
    logger.info(`Pending request for ${email} processed and cleaned up`);
  } catch (error) {
    // Leave the row in 'processing' state — startup recovery will re-attempt
    logger.error(error, `Failed to process pending request for ${email}, will retry on next startup`);
    throw error;
  }
}

await main();
