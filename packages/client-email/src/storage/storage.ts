import { Pool, PoolConfig } from 'pg';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { SQL } from 'sql-template-strings';

// =============================================================================
// Simple data types (no interface inheritance hierarchy)
// =============================================================================

export interface UserAuthToken {
  email: string;
  accessToken: string;
  refreshToken?: string;
  expiresAt: number;
  tokenType: string;
  scope?: string;
  idToken?: string;
  createdAt: number;
  updatedAt: number;
}

export interface EmailContext {
  contextKey: string;
  contextId: string;
  taskId?: string;
  subject?: string;
  senderEmail: string;
  originalMessageId?: string;
  createdAt: number;
  updatedAt: number;
}

export interface PendingRequest {
  email: string;
  subject?: string;
  bodyText?: string;
  originalMessageId?: string;
  attachmentKeys?: string[];
  status: string;
  createdAt: number;
}

export interface InFlightTask {
  taskId: string;
  contextKey: string;
  contextId?: string;
  senderEmail: string;
  subject?: string;
  originalMessageId?: string;
  webhookToken?: string;
  s3ObjectKey?: string;
  createdAt: number;
}

export interface OAuthState {
  email: string;
  teamId: string;
  codeVerifier: string;
  expiresAt: number;
}

// =============================================================================
// Storage class — single class with all DB operations
// =============================================================================

export class Storage {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger('Storage');

  constructor(config: Config) {
    const poolConfig: PoolConfig = {
      host: config.storage.postgres.host,
      port: config.storage.postgres.port,
      user: config.storage.postgres.username,
      password: config.storage.postgres.password,
      database: config.storage.postgres.database,
      max: 10,
      connectionTimeoutMillis: 30000,
      idleTimeoutMillis: 10000,
    };

    if (config.storage.postgres.useSsl) {
      poolConfig.ssl = config.storage.postgres.sslCa
        ? { rejectUnauthorized: true, ca: config.storage.postgres.sslCa }
        : { rejectUnauthorized: false };
    }

    this.pool = new Pool(poolConfig);
    this.pool.on('error', (err) => {
      this.logger.error(err, 'Unexpected error on idle PostgreSQL client');
    });

    this.logger.info(
      `Storage initialized for ${config.storage.postgres.host}:${config.storage.postgres.port}/${config.storage.postgres.database}`
    );
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

  async shutdown(): Promise<void> {
    this.logger.info('Shutting down storage...');
    await this.pool.end();
    this.logger.info('Storage shutdown complete');
  }

  // ===========================================================================
  // User Auth
  // ===========================================================================

  async saveToken(token: Omit<UserAuthToken, 'createdAt' | 'updatedAt'>): Promise<void> {
    await this.pool.query(SQL`
      INSERT INTO user_auth (email, access_token, refresh_token, expires_at, token_type, scope, id_token)
      VALUES (${token.email}, ${token.accessToken}, ${token.refreshToken},
              ${new Date(token.expiresAt)}, ${token.tokenType}, ${token.scope}, ${token.idToken})
      ON CONFLICT (email) DO UPDATE SET
        access_token = EXCLUDED.access_token,
        refresh_token = EXCLUDED.refresh_token,
        expires_at = EXCLUDED.expires_at,
        token_type = EXCLUDED.token_type,
        scope = EXCLUDED.scope,
        id_token = EXCLUDED.id_token
    `);
    this.logger.info(`Saved auth token for ${token.email}`);
  }

  async getToken(email: string): Promise<UserAuthToken | null> {
    const result = await this.pool.query(SQL`
      SELECT email, access_token, refresh_token, expires_at,
             token_type, scope, id_token, created_at, updated_at
      FROM user_auth WHERE email = ${email}
    `);
    if (result.rows.length === 0) return null;
    const row = result.rows[0];
    return {
      email: row.email,
      accessToken: row.access_token,
      refreshToken: row.refresh_token,
      expiresAt: new Date(row.expires_at).getTime(),
      tokenType: row.token_type,
      scope: row.scope,
      idToken: row.id_token,
      createdAt: new Date(row.created_at).getTime(),
      updatedAt: new Date(row.updated_at).getTime(),
    };
  }

  async deleteToken(email: string): Promise<void> {
    await this.pool.query(SQL`DELETE FROM user_auth WHERE email = ${email}`);
    this.logger.info(`Deleted auth token for ${email}`);
  }

  async updateToken(
    email: string,
    updates: {
      accessToken?: string;
      refreshToken?: string;
      expiresAt?: number;
      idToken?: string;
    }
  ): Promise<void> {
    const setClauses: string[] = [];
    const values: unknown[] = [];
    let paramIndex = 1;

    if (updates.accessToken !== undefined) {
      setClauses.push(`access_token = $${paramIndex++}`);
      values.push(updates.accessToken);
    }
    if (updates.refreshToken !== undefined) {
      setClauses.push(`refresh_token = $${paramIndex++}`);
      values.push(updates.refreshToken);
    }
    if (updates.expiresAt !== undefined) {
      setClauses.push(`expires_at = $${paramIndex++}`);
      values.push(new Date(updates.expiresAt));
    }
    if (updates.idToken !== undefined) {
      setClauses.push(`id_token = $${paramIndex++}`);
      values.push(updates.idToken);
    }

    if (setClauses.length === 0) return;

    values.push(email);
    await this.pool.query(`UPDATE user_auth SET ${setClauses.join(', ')} WHERE email = $${paramIndex}`, values);
    this.logger.info(`Updated auth token for ${email}`);
  }

  // ===========================================================================
  // Processed Email Tracking (idempotency)
  // ===========================================================================

  /**
   * Attempt to claim an email for processing. Returns true if this caller
   * "owns" it (first insert or re-claim after failure). Returns false if
   * another invocation already claimed it.
   */
  async tryClaimEmail(s3ObjectKey: string, snsMessageId?: string): Promise<boolean> {
    // INSERT if new, or UPDATE if previously failed (allow retry).
    // If row exists with status != 'failed', the ON CONFLICT does nothing → 0 rows.
    const result = await this.pool.query(SQL`
      INSERT INTO processed_email (s3_object_key, sns_message_id, status)
      VALUES (${s3ObjectKey}, ${snsMessageId ?? null}, 'processing')
      ON CONFLICT (s3_object_key) DO UPDATE SET
        status = 'processing',
        sns_message_id = COALESCE(EXCLUDED.sns_message_id, processed_email.sns_message_id)
      WHERE processed_email.status = 'failed'
      RETURNING s3_object_key
    `);
    return result.rows.length > 0;
  }

  async markEmailCompleted(s3ObjectKey: string): Promise<void> {
    await this.pool.query(SQL`
      UPDATE processed_email SET status = 'completed' WHERE s3_object_key = ${s3ObjectKey}
    `);
  }

  async markEmailFailed(s3ObjectKey: string): Promise<void> {
    await this.pool.query(SQL`
      UPDATE processed_email SET status = 'failed' WHERE s3_object_key = ${s3ObjectKey}
    `);
  }

  /**
   * Reset stuck 'processing' records older than the given age back to 'failed'
   * so they can be re-claimed on the next SNS retry or manual recovery.
   */
  async resetStuckProcessedEmails(minAgeMs: number = 5 * 60 * 1000): Promise<number> {
    const cutoff = new Date(Date.now() - minAgeMs);
    const result = await this.pool.query(SQL`
      UPDATE processed_email SET status = 'failed'
      WHERE status = 'processing' AND created_at < ${cutoff}
    `);
    return result.rowCount ?? 0;
  }

  // ===========================================================================
  // Email Context
  // ===========================================================================

  /**
   * Build a context key from sender email + subject.
   * Normalizes subject by stripping Re:/Fwd: prefixes and lowercasing.
   */
  static buildContextKey(senderEmail: string, subject: string): string {
    const normalized = subject
      .replace(/^(re|fwd?|fw):\s*/gi, '')
      .trim()
      .toLowerCase();
    return `${senderEmail.toLowerCase()}:${normalized}`;
  }

  async setContext(
    contextKey: string,
    contextId: string,
    opts?: {
      taskId?: string;
      subject?: string;
      senderEmail?: string;
      originalMessageId?: string;
    }
  ): Promise<void> {
    await this.pool.query(SQL`
      INSERT INTO email_context (context_key, context_id, task_id, subject, sender_email, original_message_id)
      VALUES (${contextKey}, ${contextId}, ${opts?.taskId ?? null}, ${opts?.subject ?? null},
              ${opts?.senderEmail ?? ''}, ${opts?.originalMessageId ?? null})
      ON CONFLICT (context_key) DO UPDATE SET
        context_id = EXCLUDED.context_id,
        task_id = COALESCE(EXCLUDED.task_id, email_context.task_id),
        original_message_id = COALESCE(EXCLUDED.original_message_id, email_context.original_message_id)
    `);
    this.logger.debug(`Saved context ${contextId} for key ${contextKey}`);
  }

  async getContext(contextKey: string): Promise<EmailContext | null> {
    const result = await this.pool.query(SQL`
      SELECT context_key, context_id, task_id, subject, sender_email,
             original_message_id, created_at, updated_at
      FROM email_context WHERE context_key = ${contextKey}
    `);
    if (result.rows.length === 0) return null;
    const row = result.rows[0];
    return {
      contextKey: row.context_key,
      contextId: row.context_id,
      taskId: row.task_id,
      subject: row.subject,
      senderEmail: row.sender_email,
      originalMessageId: row.original_message_id,
      createdAt: new Date(row.created_at).getTime(),
      updatedAt: new Date(row.updated_at).getTime(),
    };
  }

  // ===========================================================================
  // Pending Requests (stored while user completes auth)
  // ===========================================================================

  async savePendingRequest(req: Omit<PendingRequest, 'createdAt'>): Promise<void> {
    await this.pool.query(SQL`
      INSERT INTO pending_request (email, subject, body_text, original_message_id, attachment_keys, status)
      VALUES (${req.email}, ${req.subject ?? null}, ${req.bodyText ?? null},
              ${req.originalMessageId ?? null}, ${req.attachmentKeys ?? null}, ${req.status ?? 'pending'})
      ON CONFLICT (email) DO UPDATE SET
        subject = EXCLUDED.subject,
        body_text = EXCLUDED.body_text,
        original_message_id = EXCLUDED.original_message_id,
        attachment_keys = EXCLUDED.attachment_keys,
        status = EXCLUDED.status
    `);
    this.logger.debug(`Saved pending request for ${req.email}`);
  }

  async consumePendingRequest(email: string): Promise<PendingRequest | null> {
    const result = await this.pool.query(SQL`
      DELETE FROM pending_request WHERE email = ${email}
      RETURNING email, subject, body_text, original_message_id, attachment_keys, status, created_at
    `);
    if (result.rows.length === 0) return null;
    const row = result.rows[0];
    return {
      email: row.email,
      subject: row.subject,
      bodyText: row.body_text,
      originalMessageId: row.original_message_id,
      attachmentKeys: row.attachment_keys,
      status: row.status,
      createdAt: new Date(row.created_at).getTime(),
    };
  }

  /**
   * Atomically claim a pending request for processing (UPDATE status='processing').
   * Only succeeds if the current status is 'pending'. Returns null if already claimed.
   */
  async claimPendingRequest(email: string): Promise<PendingRequest | null> {
    const result = await this.pool.query(SQL`
      UPDATE pending_request SET status = 'processing'
      WHERE email = ${email} AND status = 'pending'
      RETURNING email, subject, body_text, original_message_id, attachment_keys, status, created_at
    `);
    if (result.rows.length === 0) return null;
    const row = result.rows[0];
    return {
      email: row.email,
      subject: row.subject,
      bodyText: row.body_text,
      originalMessageId: row.original_message_id,
      attachmentKeys: row.attachment_keys,
      status: row.status,
      createdAt: new Date(row.created_at).getTime(),
    };
  }

  /**
   * Delete a pending request after successful processing.
   */
  async deletePendingRequest(email: string): Promise<void> {
    await this.pool.query(SQL`DELETE FROM pending_request WHERE email = ${email}`);
    this.logger.debug(`Deleted pending request for ${email}`);
  }

  /**
   * Get all pending requests stuck in 'processing' state (for recovery on startup).
   */
  async getStuckPendingRequests(): Promise<PendingRequest[]> {
    const result = await this.pool.query(SQL`
      SELECT email, subject, body_text, original_message_id, attachment_keys, status, created_at
      FROM pending_request WHERE status = 'processing'
    `);
    return result.rows.map((row) => ({
      email: row.email,
      subject: row.subject,
      bodyText: row.body_text,
      originalMessageId: row.original_message_id,
      attachmentKeys: row.attachment_keys,
      status: row.status,
      createdAt: new Date(row.created_at).getTime(),
    }));
  }

  // ===========================================================================
  // In-Flight Tasks (async A2A tasks awaiting webhook)
  // ===========================================================================

  async saveInFlightTask(task: Omit<InFlightTask, 'createdAt'>): Promise<void> {
    await this.pool.query(SQL`
      INSERT INTO inflight_task (task_id, context_key, context_id, sender_email,
                                subject, original_message_id, webhook_token, s3_object_key)
      VALUES (${task.taskId}, ${task.contextKey}, ${task.contextId ?? null},
              ${task.senderEmail}, ${task.subject ?? null},
              ${task.originalMessageId ?? null}, ${task.webhookToken ?? null},
              ${task.s3ObjectKey ?? null})
      ON CONFLICT (task_id) DO UPDATE SET
        context_key = EXCLUDED.context_key,
        context_id = EXCLUDED.context_id,
        sender_email = EXCLUDED.sender_email,
        subject = EXCLUDED.subject,
        original_message_id = EXCLUDED.original_message_id,
        webhook_token = EXCLUDED.webhook_token,
        s3_object_key = EXCLUDED.s3_object_key
    `);
    this.logger.debug(`Saved in-flight task ${task.taskId}`);
  }

  /**
   * Update the task_id of an in-flight task (used when replacing a placeholder
   * with the real A2A task ID after dispatch).
   */
  async updateInFlightTaskId(oldTaskId: string, newTaskId: string): Promise<void> {
    await this.pool.query(SQL`
      UPDATE inflight_task SET task_id = ${newTaskId} WHERE task_id = ${oldTaskId}
    `);
    this.logger.debug(`Updated in-flight task ID: ${oldTaskId} -> ${newTaskId}`);
  }

  async getInFlightTask(taskId: string): Promise<InFlightTask | null> {
    const result = await this.pool.query(SQL`
      SELECT task_id, context_key, context_id, sender_email, subject,
             original_message_id, webhook_token, s3_object_key, created_at
      FROM inflight_task WHERE task_id = ${taskId}
    `);
    if (result.rows.length === 0) return null;
    const row = result.rows[0];
    return {
      taskId: row.task_id,
      contextKey: row.context_key,
      contextId: row.context_id,
      senderEmail: row.sender_email,
      subject: row.subject,
      originalMessageId: row.original_message_id,
      webhookToken: row.webhook_token,
      s3ObjectKey: row.s3_object_key,
      createdAt: new Date(row.created_at).getTime(),
    };
  }

  async closeInFlightTask(taskId: string): Promise<void> {
    await this.pool.query(SQL`DELETE FROM inflight_task WHERE task_id = ${taskId}`);
    this.logger.debug(`Closed in-flight task ${taskId}`);
  }

  async cleanupExpiredRecords(): Promise<void> {
    await this.pool.query(SQL`SELECT cleanup_expired_records()`);
    this.logger.debug('Cleaned up expired records');
  }

  async getAllInFlightTasks(minAgeMs: number = 2 * 60 * 1000): Promise<InFlightTask[]> {
    const cutoff = new Date(Date.now() - minAgeMs);
    const result = await this.pool.query(SQL`
      SELECT task_id, context_key, context_id, sender_email, subject,
             original_message_id, webhook_token, s3_object_key, created_at
      FROM inflight_task WHERE created_at < ${cutoff} AND expires_at > now()
    `);
    return result.rows.map((row) => ({
      taskId: row.task_id,
      contextKey: row.context_key,
      contextId: row.context_id,
      senderEmail: row.sender_email,
      subject: row.subject,
      originalMessageId: row.original_message_id,
      webhookToken: row.webhook_token,
      s3ObjectKey: row.s3_object_key,
      createdAt: new Date(row.created_at).getTime(),
    }));
  }

  // ===========================================================================
  // OAuth State
  // ===========================================================================

  async saveOAuthState(state: string, email: string, codeVerifier: string, ttlSeconds: number = 604800): Promise<void> {
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000);
    await this.pool.query(SQL`
      INSERT INTO oauth_state (state, email, team_id, code_verifier, expires_at)
      VALUES (${state}, ${email}, ${'email'}, ${codeVerifier}, ${expiresAt})
      ON CONFLICT (state) DO UPDATE SET
        email = EXCLUDED.email,
        team_id = EXCLUDED.team_id,
        code_verifier = EXCLUDED.code_verifier,
        expires_at = EXCLUDED.expires_at
    `);
    this.logger.debug(`Saved OAuth state for ${email}`);
  }

  async consumeOAuthState(state: string): Promise<OAuthState | null> {
    const result = await this.pool.query(SQL`
      DELETE FROM oauth_state WHERE state = ${state}
      RETURNING email, team_id, code_verifier, expires_at
    `);
    if (result.rows.length === 0) return null;
    const row = result.rows[0];
    const expiresAt = new Date(row.expires_at).getTime();
    if (expiresAt < Date.now()) return null;
    return {
      email: row.email,
      teamId: row.team_id,
      codeVerifier: row.code_verifier,
      expiresAt,
    };
  }
}
