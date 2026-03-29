import { StorageProvider } from './StorageProvider.js';
import { PostgresStorageProvider, type PostgresStorageConfig } from './providers/postgres/PostgresStorageProvider.js';
import { Logger } from '../utils/logger.js';

export type StorageProviderType = 'postgres';

export interface StorageConfig {
  provider: StorageProviderType;
  postgres?: PostgresStorageConfig;
}

const logger = Logger.getLogger('StorageFactory');

/**
 * Create a storage provider based on configuration.
 * Handles initialization and returns a ready-to-use provider.
 */
export async function createStorageProvider(config: StorageConfig): Promise<StorageProvider> {
  logger.info(`Creating storage provider: ${config.provider}`);

  switch (config.provider) {
    case 'postgres': {
      if (!config.postgres) {
        throw new Error('PostgreSQL storage config is required when provider is "postgres"');
      }
      return new PostgresStorageProvider(config.postgres);
    }

    default:
      throw new Error(`Unknown storage provider: ${config.provider}`);
  }
}

// Re-export types and classes for convenience
export { StorageProvider } from './StorageProvider.js';
export { PostgresStorageProvider } from './providers/postgres/PostgresStorageProvider.js';

// Re-export store types and interfaces
export type {
  UserAuthToken,
  UserAuthRecord,
  IUserAuthStorage,
  ContextRecord,
  IContextStore,
  PendingRequest,
  IPendingRequestStore,
  InFlightTask,
  IInFlightTaskStore,
  OAuthStateData,
  IOAuthStateStore,
  BotInstallation,
  IBotInstallationStore,
  AdminSession,
  IAdminSessionStore,
} from './types.js';
