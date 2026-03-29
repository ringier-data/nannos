import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';

export interface ContextRecord {
  contextKey: string;
  contextId: string;
  lastProcessedTs?: string;
  createdAt: number;
  updatedAt: number;
}

/**
 * PostgreSQL storage layer for A2A context IDs mapped to Slack threads
 */
export class PgContextStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgContextStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  /**
   * Store context ID for a Slack thread
   * @param key - Format: {teamId}:{channelId}:{threadTs}
   * @param contextId - A2A context ID
   * @param lastProcessedTs - Optional Slack message ts of the last processed message
   */
  async set(key: string, contextId: string, lastProcessedTs?: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        INSERT INTO context_store (context_key, context_id, last_processed_ts)
        VALUES (${key}, ${contextId}, ${lastProcessedTs})
        ON CONFLICT (context_key) DO UPDATE SET
          context_id = EXCLUDED.context_id,
          last_processed_ts = EXCLUDED.last_processed_ts
      `);
      this.logger.debug(`Saved context ${contextId} for key ${key}`);
    } catch (error) {
      this.logger.error(error, `Failed to save context: ${error}`);
      throw new Error(`Failed to save context: ${error}`);
    }
  }

  /**
   * Get context record for a Slack thread
   * @param key - Format: {teamId}:{channelId}:{threadTs}
   * @returns The full context record or null if not found
   */
  async get(key: string): Promise<ContextRecord | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT context_key, context_id, last_processed_ts, created_at, updated_at
        FROM context_store
        WHERE context_key = ${key}
      `);

      if (result.rows.length === 0) {
        return null;
      }

      const row = result.rows[0];
      return {
        contextKey: row.context_key,
        contextId: row.context_id,
        lastProcessedTs: row.last_processed_ts,
        createdAt: new Date(row.created_at).getTime(),
        updatedAt: new Date(row.updated_at).getTime(),
      };
    } catch (error) {
      this.logger.error(error, `Failed to get context: ${error}`);
      throw new Error(`Failed to retrieve context: ${error}`);
    }
  }

  /**
   * Delete context for a Slack thread
   * @param key - Format: {teamId}:{channelId}:{threadTs}
   */
  async delete(key: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        DELETE FROM context_store
        WHERE context_key = ${key}
      `);
      this.logger.debug(`Deleted context for key ${key}`);
    } catch (error) {
      this.logger.error(error, `Failed to delete context: ${error}`);
      throw new Error(`Failed to delete context: ${error}`);
    }
  }

  /**
   * Build key for context lookup
   */
  buildKey(teamId: string, channelId: string, threadTs: string): string {
    return `${teamId}:${channelId}:${threadTs}`;
  }
}
