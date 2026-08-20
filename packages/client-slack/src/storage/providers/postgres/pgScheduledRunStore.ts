import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';
import type { IScheduledRunStore, ScheduledRunRecord } from '../../types.js';

/**
 * PostgreSQL storage layer mapping delivered scheduled-run notifications to
 * their provenance (job, run, sub-agent, and the run's A2A context id).
 * Keyed by the delivered Slack message: {teamId}:{channelId}:{messageTs}.
 */
export class PgScheduledRunStore implements IScheduledRunStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgScheduledRunStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  async set(record: ScheduledRunRecord): Promise<void> {
    try {
      await this.pool.query(SQL`
        INSERT INTO scheduled_run_store (
          context_key, context_id, scheduled_job_id, scheduled_job_run_id,
          sub_agent_id, sub_agent_name, prompt, result_summary
        )
        VALUES (
          ${record.contextKey}, ${record.contextId}, ${record.scheduledJobId}, ${record.scheduledJobRunId},
          ${record.subAgentId}, ${record.subAgentName}, ${record.prompt}, ${record.resultSummary}
        )
        ON CONFLICT (context_key) DO UPDATE SET
          context_id = EXCLUDED.context_id,
          scheduled_job_id = EXCLUDED.scheduled_job_id,
          scheduled_job_run_id = EXCLUDED.scheduled_job_run_id,
          sub_agent_id = EXCLUDED.sub_agent_id,
          sub_agent_name = EXCLUDED.sub_agent_name,
          prompt = EXCLUDED.prompt,
          result_summary = EXCLUDED.result_summary
      `);
      this.logger.debug(`Saved scheduled-run provenance for key ${record.contextKey}`);
    } catch (error) {
      this.logger.error(error, `Failed to save scheduled-run provenance: ${error}`);
      throw new Error(`Failed to save scheduled-run provenance: ${error}`);
    }
  }

  async get(key: string): Promise<ScheduledRunRecord | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT context_key, context_id, scheduled_job_id, scheduled_job_run_id,
               sub_agent_id, sub_agent_name, prompt, result_summary
        FROM scheduled_run_store
        WHERE context_key = ${key}
      `);

      if (result.rows.length === 0) {
        return null;
      }

      const row = result.rows[0];
      return {
        contextKey: row.context_key,
        contextId: row.context_id,
        scheduledJobId: row.scheduled_job_id !== null ? Number(row.scheduled_job_id) : undefined,
        scheduledJobRunId: row.scheduled_job_run_id !== null ? Number(row.scheduled_job_run_id) : undefined,
        subAgentId: row.sub_agent_id !== null ? Number(row.sub_agent_id) : undefined,
        subAgentName: row.sub_agent_name ?? undefined,
        prompt: row.prompt ?? undefined,
        resultSummary: row.result_summary ?? undefined,
      };
    } catch (error) {
      this.logger.error(error, `Failed to get scheduled-run provenance: ${error}`);
      throw new Error(`Failed to retrieve scheduled-run provenance: ${error}`);
    }
  }

  buildKey(teamId: string, channelId: string, messageTs: string): string {
    return `${teamId}:${channelId}:${messageTs}`;
  }
}
