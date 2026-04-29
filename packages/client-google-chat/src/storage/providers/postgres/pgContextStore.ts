import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';

export interface ContextRecord {
  contextKey: string;
  contextId: string;
  lastProcessedMessageId?: string;
  createdAt: number;
  updatedAt: number;
}

export class PgContextStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgContextStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  async set(key: string, contextId: string, lastProcessedMessageId?: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        INSERT INTO context_store (context_key, context_id, last_processed_message_id)
        VALUES (${key}, ${contextId}, ${lastProcessedMessageId})
        ON CONFLICT (context_key) DO UPDATE SET
          context_id = EXCLUDED.context_id,
          last_processed_message_id = EXCLUDED.last_processed_message_id
      `);
    } catch (error) {
      this.logger.error(error, `Failed to save context: ${error}`);
      throw new Error(`Failed to save context: ${error}`);
    }
  }

  async get(key: string): Promise<ContextRecord | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT context_key, context_id, last_processed_message_id, created_at, updated_at
        FROM context_store WHERE context_key = ${key}
      `);
      if (result.rows.length === 0) return null;
      const row = result.rows[0];
      return {
        contextKey: row.context_key,
        contextId: row.context_id,
        lastProcessedMessageId: row.last_processed_message_id,
        createdAt: new Date(row.created_at).getTime(),
        updatedAt: new Date(row.updated_at).getTime(),
      };
    } catch (error) {
      this.logger.error(error, `Failed to get context: ${error}`);
      throw new Error(`Failed to retrieve context: ${error}`);
    }
  }

  async delete(key: string): Promise<void> {
    try {
      await this.pool.query(SQL`DELETE FROM context_store WHERE context_key = ${key}`);
    } catch (error) {
      this.logger.error(error, `Failed to delete context: ${error}`);
      throw new Error(`Failed to delete context: ${error}`);
    }
  }

  /**
   * Build context key from space and thread identifiers.
   */
  buildKey(projectId: string, spaceId: string, threadId: string): string {
    return `${projectId}:${spaceId}:${threadId}`;
  }
}
