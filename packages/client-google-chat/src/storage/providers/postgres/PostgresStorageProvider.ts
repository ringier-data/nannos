import { Pool, PoolConfig } from 'pg';
import { StorageProvider } from '../../StorageProvider.js';
import { PgUserAuthStorage } from './pgUserAuthStorage.js';
import { PgContextStore } from './pgContextStore.js';
import { PgPendingRequestStore } from './pgPendingRequestStore.js';
import { PgInFlightTaskStore } from './pgInFlightTaskStore.js';
import { PgOAuthStateStore } from './pgOAuthStateStore.js';
import { Logger } from '../../../utils/logger.js';

export interface PostgresStorageConfig {
  host: string;
  port: number;
  username: string;
  password: string;
  database: string;
  useSsl: boolean;
  sslCa?: string;
  maxPoolSize?: number;
  connectionTimeoutMs?: number;
  idleTimeoutMs?: number;
}

export class PostgresStorageProvider extends StorageProvider {
  private readonly logger = Logger.getLogger(PostgresStorageProvider.name);
  private readonly pool: Pool;

  readonly userAuth: PgUserAuthStorage;
  readonly context: PgContextStore;
  readonly pendingRequest: PgPendingRequestStore;
  readonly inFlightTask: PgInFlightTaskStore;
  readonly oauthState: PgOAuthStateStore;

  constructor(config: PostgresStorageConfig) {
    super();
    const poolConfig: PoolConfig = {
      host: config.host,
      port: config.port,
      user: config.username,
      password: config.password,
      database: config.database,
      max: config.maxPoolSize ?? 10,
      connectionTimeoutMillis: config.connectionTimeoutMs ?? 30000,
      idleTimeoutMillis: config.idleTimeoutMs ?? 10000,
    };
    if (config.useSsl) {
      poolConfig.ssl = config.sslCa ? { rejectUnauthorized: true, ca: config.sslCa } : { rejectUnauthorized: false };
    }
    this.pool = new Pool(poolConfig);
    this.pool.on('error', (err) => {
      this.logger.error(err, 'Unexpected error on idle PostgreSQL client');
    });
    this.userAuth = new PgUserAuthStorage(this.pool);
    this.context = new PgContextStore(this.pool);
    this.pendingRequest = new PgPendingRequestStore(this.pool);
    this.inFlightTask = new PgInFlightTaskStore(this.pool);
    this.oauthState = new PgOAuthStateStore(this.pool);
    this.logger.info(`PostgreSQL storage provider initialized for ${config.host}:${config.port}/${config.database}`);
  }

  async testConnection(): Promise<boolean> {
    try {
      const client = await this.pool.connect();
      try {
        await client.query('SELECT 1');
        this.logger.info('PostgreSQL connection test successful');
        return true;
      } finally {
        client.release();
      }
    } catch (error) {
      this.logger.error(error, 'PostgreSQL connection test failed');
      return false;
    }
  }

  getPoolStats(): { total: number; idle: number; waiting: number } {
    return {
      total: this.pool.totalCount,
      idle: this.pool.idleCount,
      waiting: this.pool.waitingCount,
    };
  }

  async shutdown(): Promise<void> {
    this.logger.info('Shutting down PostgreSQL storage provider...');
    await this.pool.end();
    this.logger.info('PostgreSQL storage provider shutdown complete');
  }
}
