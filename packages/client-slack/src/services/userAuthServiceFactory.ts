/**
 * createUserAuthService
 * ---------------------
 * Selects who runs the per-user sign-in from `USER_AUTH_MODE`, mirroring the
 * installation-secret factory.
 *
 *   'local'  (default) — this client's own Keycloak login.
 *   'broker'           — console-backend's token broker, with old local sign-ins served
 *                        until they drain (CompositeUserAuthService).
 */

import type { Config } from '../config/config.js';
import type { StorageProvider } from '../storage/index.js';
import type { BotInstallation } from '../storage/types.js';
import { BrokerClient } from './brokerClient.js';
import { BrokerUserAuthService } from './brokerUserAuthService.js';
import { CompositeUserAuthService } from './compositeUserAuthService.js';
import type { OIDCClient } from './oidcClient.js';
import { IUserAuthService, LocalUserAuthService } from './userAuthService.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('UserAuthServiceFactory');

export function createUserAuthService(
  config: Config,
  storage: Pick<StorageProvider, 'userAuth' | 'oauthState' | 'botInstallation'>,
  oidcClient: OIDCClient
): IUserAuthService {
  const local = new LocalUserAuthService(storage.userAuth, oidcClient, config, storage.oauthState);
  logger.info(`Creating user auth service: mode=${config.userAuthMode}`);

  switch (config.userAuthMode) {
    case 'local':
      return local;

    case 'broker': {
      if (!config.consoleBackend) {
        throw new Error('USER_AUTH_MODE=broker requires CONSOLE_BACKEND_URL');
      }
      // Sign-in links go to the browser, so they need the public URL.
      logger.info(`Broker sign-in links point at ${config.consoleBackend.publicUrl}`);
      const broker = createBrokerClient(config, oidcClient);
      return new CompositeUserAuthService(
        storage.userAuth,
        local,
        new BrokerUserAuthService(storage.userAuth, broker, config, storage.oauthState, async (teamId) =>
          activeAppIds(await storage.botInstallation.getByTeamId(teamId))
        )
      );
    }

    default:
      throw new Error(`Unknown user auth mode: ${config.userAuthMode}`);
  }
}

/** The token broker client, as this client itself calls console-backend. Broker mode only. */
export function createBrokerClient(config: Config, oidcClient: OIDCClient): BrokerClient {
  if (!config.consoleBackend) {
    throw new Error('USER_AUTH_MODE=broker requires CONSOLE_BACKEND_URL');
  }
  return new BrokerClient({
    baseUrl: config.consoleBackend.url,
    publicBaseUrl: config.consoleBackend.publicUrl,
    clientId: config.oidc.clientId,
    serviceAudience: config.consoleBackend.audience,
    getServiceCredentials: (audience) => oidcClient.getServiceCredentials(audience),
  });
}

/**
 * The installations a team's sign-ins can be reached on: the apps active there. A sign-in is
 * per team and a notification is looked up by team, so every one of them reaches the user.
 */
export function activeAppIds(bots: BotInstallation[]): string[] {
  return bots.filter((bot) => bot.isActive).map((bot) => bot.appId);
}

