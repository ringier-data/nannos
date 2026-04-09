import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';

/**
 * In-flight task data structure
 * Stores context needed to post results back to Slack when A2A completes
 */
export interface InFlightTask {
  taskId: string; // PK: A2A task ID
  visitorId: string; // {teamId}:{userId} for querying by user
  userId: string; // Slack user ID
  teamId: string; // Slack team/workspace ID
  channelId: string; // Slack channel ID
  threadTs: string; // Thread timestamp to reply to
  messageTs: string; // Original message timestamp (for reactions)
  statusMessageTs?: string; // Status message timestamp (for updates)
  contextKey: string; // Context store key for conversation continuity
  webhookToken?: string; // Token for validating A2A push notifications
  source: 'app_mention' | 'direct_message';
  appId?: string; // Slack App ID that received the message (for multi-bot token routing)
  createdAt: number;
  lastActivityAt: Date;
  ttl: number; // Unix timestamp (seconds) for cleanup - kept for interface compatibility
}

/**
 * PostgreSQL storage layer for in-flight A2A tasks
 * Used to store Slack context while waiting for A2A webhook callback
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
  buildVisitorId(teamId: string, userId: string): string {
    return `${teamId}:${userId}`;
  }

  /**
   * Store an in-flight task
   */
  async save(task: Omit<InFlightTask, 'ttl'>): Promise<void> {
    const expiresAt = new Date(Date.now() + this.TTL_SECONDS * 1000);

    try {
      await this.pool.query(SQL`
        INSERT INTO inflight_tasks (
          task_id, visitor_id, user_id, team_id, channel_id,
          thread_ts, message_ts, status_message_ts, context_key,
          webhook_token, source, app_id, expires_at
        ) VALUES (
          ${task.taskId}, ${task.visitorId}, ${task.userId}, ${task.teamId},
          ${task.channelId}, ${task.threadTs}, ${task.messageTs}, ${task.statusMessageTs},
          ${task.contextKey}, ${task.webhookToken}, ${task.source}, ${task.appId ?? null}, ${expiresAt}
        )
        ON CONFLICT (task_id) DO UPDATE SET
          visitor_id = EXCLUDED.visitor_id,
          user_id = EXCLUDED.user_id,
          team_id = EXCLUDED.team_id,
          channel_id = EXCLUDED.channel_id,
          thread_ts = EXCLUDED.thread_ts,
          message_ts = EXCLUDED.message_ts,
          status_message_ts = EXCLUDED.status_message_ts,
          context_key = EXCLUDED.context_key,
          webhook_token = EXCLUDED.webhook_token,
          source = EXCLUDED.source,
          app_id = EXCLUDED.app_id,
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
        SELECT task_id, visitor_id, user_id, team_id, channel_id,
               thread_ts, message_ts, status_message_ts, context_key,
               webhook_token, source, app_id, created_at, expires_at
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
   * Update status message timestamp for a task
   */
  async updateStatusMessageTs(taskId: string, statusMessageTs: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        UPDATE inflight_tasks
        SET status_message_ts = ${statusMessageTs}
        WHERE task_id = ${taskId}
      `);
      this.logger.debug(`Updated statusMessageTs for task ${taskId}`);
    } catch (error) {
      this.logger.error(error, `Failed to update statusMessageTs: ${error}`);
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
  async getByUser(teamId: string, userId: string): Promise<InFlightTask[]> {
    const visitorId = this.buildVisitorId(teamId, userId);

    try {
      const result = await this.pool.query(SQL`
        SELECT task_id, visitor_id, user_id, team_id, channel_id,
               thread_ts, message_ts, status_message_ts, context_key,
               webhook_token, source, app_id, created_at, expires_at
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
        SELECT task_id, visitor_id, user_id, team_id, channel_id,
               thread_ts, message_ts, status_message_ts, context_key,
               webhook_token, source, app_id, created_at, expires_at, last_activity_at
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
      teamId: row.team_id,
      channelId: row.channel_id,
      threadTs: row.thread_ts,
      messageTs: row.message_ts,
      statusMessageTs: row.status_message_ts,
      contextKey: row.context_key,
      webhookToken: row.webhook_token,
      source: row.source,
      appId: row.app_id ?? undefined,
      lastActivityAt: new Date(row.last_activity_at),
      createdAt: new Date(row.created_at).getTime(),
      ttl: Math.floor(new Date(row.expires_at).getTime() / 1000),
    };
  }
}
