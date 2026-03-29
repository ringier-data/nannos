import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';

/**
 * Pending request data structure
 */
export interface PendingRequest {
  visitorId: string; // PK: {teamId}:{userId}
  text: string;
  channelId: string;
  threadTs: string;
  messageTs: string;
  source: 'app_mention' | 'direct_message';
  appId?: string; // Slack App ID that received the original message
  createdAt: number;
}

/**
 * PostgreSQL storage layer for pending requests (requests made before user authorized)
 */
export class PgPendingRequestStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgPendingRequestStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  /**
   * Build the visitor ID key
   */
  buildVisitorId(teamId: string, userId: string): string {
    return `${teamId}:${userId}`;
  }

  /**
   * Store a pending request for a user
   */
  async set(request: PendingRequest): Promise<void> {
    try {
      await this.pool.query(SQL`
        INSERT INTO pending_requests (
          visitor_id, text, channel_id, thread_ts, message_ts, source, app_id
        ) VALUES (
          ${request.visitorId}, ${request.text}, ${request.channelId},
          ${request.threadTs}, ${request.messageTs}, ${request.source}, ${request.appId ?? null}
        )
        ON CONFLICT (visitor_id) DO UPDATE SET
          text = EXCLUDED.text,
          channel_id = EXCLUDED.channel_id,
          thread_ts = EXCLUDED.thread_ts,
          message_ts = EXCLUDED.message_ts,
          source = EXCLUDED.source,
          app_id = EXCLUDED.app_id
      `);
      this.logger.debug(`Saved pending request for ${request.visitorId}`);
    } catch (error) {
      this.logger.error(error, `Failed to save pending request: ${error}`);
      throw new Error(`Failed to save pending request: ${error}`);
    }
  }

  /**
   * Get and remove pending request for a user (one-time use)
   */
  async consume(teamId: string, userId: string): Promise<PendingRequest | null> {
    const visitorId = this.buildVisitorId(teamId, userId);

    try {
      const result = await this.pool.query(SQL`
        DELETE FROM pending_requests
        WHERE visitor_id = ${visitorId}
        RETURNING visitor_id, text, channel_id, thread_ts, message_ts, source, app_id, created_at
      `);

      if (result.rows.length === 0) {
        return null;
      }

      const row = result.rows[0];
      return {
        visitorId: row.visitor_id,
        text: row.text,
        channelId: row.channel_id,
        threadTs: row.thread_ts,
        messageTs: row.message_ts,
        source: row.source,
        appId: row.app_id ?? undefined,
        createdAt: new Date(row.created_at).getTime(),
      };
    } catch (error) {
      this.logger.error(error, `Failed to get pending request: ${error}`);
      throw new Error(`Failed to retrieve pending request: ${error}`);
    }
  }

  /**
   * Delete pending request for a user
   */
  async delete(teamId: string, userId: string): Promise<void> {
    const visitorId = this.buildVisitorId(teamId, userId);

    try {
      await this.pool.query(SQL`
        DELETE FROM pending_requests
        WHERE visitor_id = ${visitorId}
      `);
      this.logger.debug(`Deleted pending request for ${visitorId}`);
    } catch (error) {
      this.logger.error(error, `Failed to delete pending request: ${error}`);
      // Don't throw - deletion failure shouldn't break the flow
    }
  }
}
