import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';

/**
 * In-flight task data structure
 * Stores context needed to post results back to Google Chat when A2A completes
 */
export interface InFlightTask {
  taskId: string; // PK: A2A task ID
  visitorId: string; // {projectId}:{userId} for querying by user
  userId: string; // Google Chat user ID
  projectId: string; // Google chat project number
  spaceId: string; // Google Chat space ID
  threadId: string; // Thread key/name to reply in
  messageId: string; // Original message name (for updates)
  statusMessageId?: string; // Status message name (for updates)
  contextKey: string; // Context store key for conversation continuity
  webhookToken?: string; // Token for validating A2A push notifications
  source: 'space_message' | 'direct_message';
  createdAt: number;
  lastActivityAt: Date;
  ttl: number; // Unix timestamp (seconds) for cleanup - kept for interface compatibility
}

/**
 * PostgreSQL storage layer for in-flight A2A tasks
 * Used to store Google Chat context while waiting for A2A webhook callback
 */
export class PgInFlightTaskStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgInFlightTaskStore.name);

  // Tasks expire after 1 hour (A2A should complete well before this)
  private readonly TTL_SECONDS = 60 * 60;

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
   * Store an in-flight task
   */
  async save(task: Omit<InFlightTask, 'ttl'>): Promise<void> {
    const expiresAt = new Date(Date.now() + this.TTL_SECONDS * 1000);

    try {
      await this.pool.query(SQL`
        INSERT INTO inflight_tasks (
          task_id, visitor_id, user_id, project_id, space_id,
          thread_id, message_id, status_message_id, context_key,
          webhook_token, source, expires_at
        ) VALUES (
          ${task.taskId}, ${task.visitorId}, ${task.userId}, ${task.projectId},
          ${task.spaceId}, ${task.threadId}, ${task.messageId}, ${task.statusMessageId},
          ${task.contextKey}, ${task.webhookToken}, ${task.source}, ${expiresAt}
        )
        ON CONFLICT (task_id) DO UPDATE SET
          visitor_id = EXCLUDED.visitor_id,
          user_id = EXCLUDED.user_id,
          project_id = EXCLUDED.project_id,
          space_id = EXCLUDED.space_id,
          thread_id = EXCLUDED.thread_id,
          message_id = EXCLUDED.message_id,
          status_message_id = EXCLUDED.status_message_id,
          context_key = EXCLUDED.context_key,
          webhook_token = EXCLUDED.webhook_token,
          source = EXCLUDED.source,
          expires_at = EXCLUDED.expires_at
      `);
      this.logger.debug(`Saved in-flight task ${task.taskId} for ${task.visitorId}`);
    } catch (error) {
      this.logger.error(error, `Failed to save in-flight task: ${error}`);
      throw new Error(`Failed to save in-flight task: ${error}`);
    }
  }

  /**
   * Get in-flight task by task ID
   */
  async get(taskId: string): Promise<InFlightTask | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT task_id, visitor_id, user_id, project_id, space_id,
               thread_id, message_id, status_message_id, context_key,
               webhook_token, source, created_at, expires_at
        FROM inflight_tasks
        WHERE task_id = ${taskId}
      `);

      if (result.rows.length === 0) {
        return null;
      }

      return this.rowToTask(result.rows[0]);
    } catch (error) {
      this.logger.error(error, `Failed to get in-flight task: ${error}`);
      throw new Error(`Failed to retrieve in-flight task: ${error}`);
    }
  }

  /**
   * Update status message ID for a task
   */
  async updateStatusMessageId(taskId: string, statusMessageId: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        UPDATE inflight_tasks
        SET status_message_id = ${statusMessageId}
        WHERE task_id = ${taskId}
      `);
      this.logger.debug(`Updated statusMessageId for task ${taskId}`);
    } catch (error) {
      this.logger.error(error, `Failed to update statusMessageId: ${error}`);
      // Don't throw - this is a non-critical update
    }
  }

  /**
   * Delete in-flight task by task ID
   */
  async delete(taskId: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        DELETE FROM inflight_tasks
        WHERE task_id = ${taskId}
      `);
      this.logger.debug(`Deleted in-flight task ${taskId}`);
    } catch (error) {
      this.logger.error(error, `Failed to delete in-flight task: ${error}`);
      // Don't throw - deletion failure shouldn't break the flow
    }
  }

  /**
   * Get all in-flight tasks for a user
   */
  async getByUser(projectId: string, userId: string): Promise<InFlightTask[]> {
    const visitorId = this.buildVisitorId(projectId, userId);

    try {
      const result = await this.pool.query(SQL`
        SELECT task_id, visitor_id, user_id, project_id, space_id,
               thread_id, message_id, status_message_id, context_key,
               webhook_token, source, created_at, expires_at
        FROM inflight_tasks
        WHERE visitor_id = ${visitorId}
      `);

      return result.rows.map((row) => this.rowToTask(row));
    } catch (error) {
      this.logger.error(error, `Failed to query in-flight tasks by user: ${error}`);
      return [];
    }
  }

  /**
   * Get all in-flight tasks
   * @param minAgeMs - Only return tasks with no activity in the last many milliseconds (default: 10 minutes)
   */
  async getAll(minAgeMs: number = 10 * 60 * 1000): Promise<InFlightTask[]> {
    const cutoffTime = new Date(Date.now() - minAgeMs);

    try {
      const result = await this.pool.query(SQL`
        SELECT task_id, visitor_id, user_id, project_id, space_id,
               thread_id, message_id, status_message_id, context_key,
               webhook_token, source, created_at, expires_at, last_activity_at
        FROM inflight_tasks
        WHERE last_activity_at < ${cutoffTime}
      `);

      const tasks = result.rows.map((row) => this.rowToTask(row));
      return tasks;
    } catch (error) {
      this.logger.error(error, `Failed to scan in-flight tasks: ${error}`);
      return [];
    }
  }

  async touch(taskId: string): Promise<void> {
    await this.pool.query(SQL`
        UPDATE inflight_tasks
        SET last_activity_at = now()
        WHERE task_id = ${taskId}
      `);
  }

  /**
   * Convert a database row to an InFlightTask
   */
  private rowToTask(row: any): InFlightTask {
    return {
      taskId: row.task_id,
      visitorId: row.visitor_id,
      userId: row.user_id,
      projectId: row.project_id,
      spaceId: row.space_id,
      threadId: row.thread_id,
      messageId: row.message_id,
      statusMessageId: row.status_message_id,
      contextKey: row.context_key,
      webhookToken: row.webhook_token,
      source: row.source,
      createdAt: new Date(row.created_at).getTime(),
      lastActivityAt: new Date(row.last_activity_at),
      ttl: Math.floor(new Date(row.expires_at).getTime() / 1000),
    };
  }
}
