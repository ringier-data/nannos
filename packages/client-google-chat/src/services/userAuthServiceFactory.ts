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
import { BrokerClient } from './brokerClient.js';
import { BrokerUserAuthService } from './brokerUserAuthService.js';
import { CompositeUserAuthService } from './compositeUserAuthService.js';
import type { OIDCClient } from './oidcClient.js';
import { IUserAuthService, LocalUserAuthService } from './userAuthService.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('UserAuthServiceFactory');

export function createUserAuthService(
  config: Config,
  storage: Pick<StorageProvider, 'userAuth' | 'oauthState'>,
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
      const broker = new BrokerClient({
        baseUrl: config.consoleBackend.url,
        clientId: config.oidc.clientId,
        serviceAudience: config.consoleBackend.audience,
        getServiceCredentials: (audience) => oidcClient.getServiceCredentials(audience),
      });
      return new CompositeUserAuthService(
        storage.userAuth,
        local,
        new BrokerUserAuthService(storage.userAuth, broker, config, storage.oauthState)
      );
    }

    default:
      throw new Error(`Unknown user auth mode: ${config.userAuthMode}`);
  }
}
