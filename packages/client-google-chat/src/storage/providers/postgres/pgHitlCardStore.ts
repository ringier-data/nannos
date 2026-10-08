import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';

/** A card stays answerable for a day; an older one is not worth settling. */
const DEFAULT_TTL_SECONDS = 24 * 3600;

/**
 * PostgreSQL storage for the approval cards a typed answer may have to settle:
 * call id → the card message that asks about it.
 */
export class PgHitlCardStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgHitlCardStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  /** Remember that *messageName* is the card asking about *callIds*. */
  async set(projectId: string, messageName: string, callIds: string[], ttlSeconds = DEFAULT_TTL_SECONDS): Promise<void> {
    if (callIds.length === 0) return;
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000);
    for (const callId of callIds) {
      await this.pool.query(SQL`
        INSERT INTO hitl_card_message (project_id, call_id, message_name, expires_at)
        VALUES (${projectId}, ${callId}, ${messageName}, ${expiresAt})
        ON CONFLICT (project_id, call_id) DO UPDATE SET
          message_name = EXCLUDED.message_name,
          expires_at = EXCLUDED.expires_at
      `);
    }
    this.logger.debug(`Saved approval card ${messageName} for ${callIds.length} call(s)`);
  }

  /**
   * The cards asking about *callIds*, removed as they are returned: each is settled
   * once. Expired rows are dropped on the way.
   */
  async take(projectId: string, callIds: string[]): Promise<string[]> {
    if (callIds.length === 0) return [];
    await this.pool.query(SQL`DELETE FROM hitl_card_message WHERE expires_at < now()`);
    const result = await this.pool.query(SQL`
      DELETE FROM hitl_card_message
      WHERE project_id = ${projectId} AND call_id = ANY(${callIds})
      RETURNING message_name
    `);
    return [...new Set(result.rows.map((row: { message_name: string }) => row.message_name))];
  }
}
