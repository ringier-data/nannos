import type {
  IUserAuthStorage,
  IContextStore,
  IPendingRequestStore,
  IInFlightTaskStore,
  IOAuthStateStore,
  IBotInstallationStore,
  IAdminSessionStore,
} from './types.js';

/**
 * Abstract base class for storage providers.
 * Implementations provide all storage stores for the application.
 */
export abstract class StorageProvider {
  abstract readonly userAuth: IUserAuthStorage;
  abstract readonly context: IContextStore;
  abstract readonly pendingRequest: IPendingRequestStore;
  abstract readonly inFlightTask: IInFlightTaskStore;
  abstract readonly oauthState: IOAuthStateStore;
  abstract readonly botInstallation: IBotInstallationStore;
  abstract readonly adminSession: IAdminSessionStore;

  /**
   * Gracefully shutdown the storage provider.
   * Called on SIGINT/SIGTERM to clean up connections.
   */
  abstract shutdown(): Promise<void>;
}
