import 'dotenv/config.js';
import { Server } from 'http';
import { Config, getConfigFromEnv } from './config/config.js';
import { startSlackApp } from './slackApp.js';
import { Logger } from './utils/logger.js';
import { getV2App } from './v2App.js';
import { createStorageProvider } from './storage/index.js';

async function main() {
  const config = await getConfigFromEnv();
  const logger = Logger.getLogger('main');
  logger.info('Starting application');

  const storage = await createStorageProvider(config.storage);
  const v2App = getV2App(config, storage);
  const server = v2App.listen(config.v2AppPort, () => {
    logger.info(`Server is running on port ${config.v2AppPort}`);
  });

  let slackServer: { stop: () => Promise<unknown> };
  if (process.env.SKIP_SLACK !== 'true') {
    slackServer = await startSlackApp(config);
  } else {
    logger.info('SKIP_SLACK is set to true, skipping Slack app initialization');
    slackServer = { stop: async () => Promise.resolve() };
  }

  setupServerTimeouts(server, config);
  process.on('SIGTERM', () => {
    logger.info('SIGTERM received, shutting down gracefully');
    shutdown(slackServer, server, logger);
  });
  process.on('SIGINT', async () => {
    logger.info('SIGINT received, shutting down gracefully');
    await shutdown(slackServer, server, logger);
  });
  process.on('uncaughtException', handleError);
  process.on('unhandledRejection', handleError);
  function handleError(err: Error) {
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
  }
}
function setupServerTimeouts(server: Server, config: Config) {
  // Ensure all inactive connections are terminated by the ALB, by setting this a few seconds higher than the ALB idle timeout
  server.keepAliveTimeout = config.httpKeepAliveTimeout;
  // Ensure the headersTimeout is set higher than the keepAliveTimeout due to this nodejs regression bug: https://github.com/nodejs/node/issues/27363
  server.headersTimeout = config.httpKeepAliveTimeout + 2e3;
  // Ensure TCP timeout does not happen before
  server.timeout = config.httpKeepAliveTimeout + 4e3;
}

function shutdown(slackServer: { stop: () => Promise<unknown> }, v2Server: Server, logger: Logger) {
  logger.info('Closing server');

  const v1stop = slackServer.stop();
  const v2stop = v2Server.close();
  Promise.all([v1stop, v2stop]).then(
    () => {
      process.exit(0);
    },
    (err) => {
      logger.error(err, 'Error during shutdown');
      process.exit(1);
    }
  );
}

// eslint-disable-next-line no-console
main().catch((x) => console.error(x));
