/**
 * createUserAuthService
 * ---------------------
 * Selects who runs the per-user sign-in from `USER_AUTH_MODE`.
 *
 *   'local'  (default) — this client's own Keycloak login.
 *   'broker'           — console-backend's token broker, with old local sign-ins served
 *                        until they drain (CompositeUserAuthService).
 */

import type { Config } from '../config/config.js';
import type { Storage } from '../storage/storage.js';
import { BrokerClient } from './brokerClient.js';
import { BrokerUserAuthService } from './brokerUserAuthService.js';
import { CompositeUserAuthService } from './compositeUserAuthService.js';
import type { OIDCClient } from './oidcClient.js';
import { type IUserAuthService, LocalUserAuthService } from './userAuthService.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('UserAuthServiceFactory');

export function createUserAuthService(config: Config, storage: Storage, oidcClient: OIDCClient): IUserAuthService {
  const local = new LocalUserAuthService(storage, oidcClient, config);
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
      return new CompositeUserAuthService(storage, local, new BrokerUserAuthService(storage, broker, config));
    }

    default:
      throw new Error(`Unknown user auth mode: ${config.userAuthMode}`);
  }
}
