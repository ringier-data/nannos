import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';

/**
 * Pending request data structure
 */
export interface PendingRequest {
  visitorId: string; // PK: {projectId}:{userId}
  text: string;
  spaceId: string;
  threadId: string;
  messageId: string;
  userEmail: string;
  source: 'space_message' | 'direct_message';
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
  buildVisitorId(projectId: string, userId: string): string {
    return `${projectId}:${userId}`;
  }

  /**
   * Store a pending request for a user
   */
  async set(request: PendingRequest): Promise<void> {
    try {
      await this.pool.query(SQL`
        INSERT INTO pending_requests (
          visitor_id, text, space_id, thread_id, message_id, user_email, source
        ) VALUES (
          ${request.visitorId}, ${request.text}, ${request.spaceId},
          ${request.threadId}, ${request.messageId}, ${request.userEmail}, ${request.source}
        )
        ON CONFLICT (visitor_id) DO UPDATE SET
          text = EXCLUDED.text,
          space_id = EXCLUDED.space_id,
          thread_id = EXCLUDED.thread_id,
          message_id = EXCLUDED.message_id,
          user_email = EXCLUDED.user_email,
          source = EXCLUDED.source
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
  async consume(projectId: string, userId: string): Promise<PendingRequest | null> {
    const visitorId = this.buildVisitorId(projectId, userId);

    try {
      const result = await this.pool.query(SQL`
        DELETE FROM pending_requests
        WHERE visitor_id = ${visitorId}
        RETURNING visitor_id, text, space_id, thread_id, message_id, user_email, source, created_at
      `);

      if (result.rows.length === 0) {
        return null;
      }

      const row = result.rows[0];
      return {
        visitorId: row.visitor_id,
        text: row.text,
        spaceId: row.space_id,
        threadId: row.thread_id,
        messageId: row.message_id,
        userEmail: row.user_email,
        source: row.source,
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
  async delete(projectId: string, userId: string): Promise<void> {
    const visitorId = this.buildVisitorId(projectId, userId);

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
