import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';

export interface OAuthStateData {
  userId: string;
  teamId: string;
  codeVerifier: string;
  expiresAt: number;
}

/**
 * PostgreSQL storage layer for OAuth state management.
 * Stores PKCE code verifiers and user context during OAuth flow.
 */
export class PgOAuthStateStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgOAuthStateStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  /**
   * Store OAuth state
   */
  async set(
    state: string,
    userId: string,
    teamId: string,
    codeVerifier: string,
    ttlSeconds: number = 600
  ): Promise<void> {
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000);

    try {
      await this.pool.query(SQL`
        INSERT INTO oauth_state (state, user_id, team_id, code_verifier, expires_at)
        VALUES (${state}, ${userId}, ${teamId}, ${codeVerifier}, ${expiresAt})
        ON CONFLICT (state) DO UPDATE SET
          user_id = EXCLUDED.user_id,
          team_id = EXCLUDED.team_id,
          code_verifier = EXCLUDED.code_verifier,
          expires_at = EXCLUDED.expires_at
      `);
      this.logger.debug(`Saved OAuth state for user ${userId}`);
    } catch (error) {
      this.logger.error(error, `Failed to save OAuth state: ${error}`);
      throw new Error(`Failed to save OAuth state: ${error}`);
    }
  }

  /**
   * Get OAuth state without consuming it (for validation)
   */
  async get(state: string): Promise<OAuthStateData | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT user_id, team_id, code_verifier, expires_at
        FROM oauth_state
        WHERE state = ${state}
      `);

      if (result.rows.length === 0) {
        return null;
      }

      const row = result.rows[0];
      const expiresAt = new Date(row.expires_at).getTime();

      // Check if expired
      if (expiresAt < Date.now()) {
        // Clean up expired record
        await this.delete(state);
        return null;
      }

      return {
        userId: row.user_id,
        teamId: row.team_id,
        codeVerifier: row.code_verifier,
        expiresAt,
      };
    } catch (error) {
      this.logger.error(error, `Failed to get OAuth state: ${error}`);
      throw new Error(`Failed to retrieve OAuth state: ${error}`);
    }
  }

  /**
   * Get and remove OAuth state (one-time use)
   */
  async consume(state: string): Promise<{ userId: string; teamId: string; codeVerifier: string } | null> {
    try {
      const result = await this.pool.query(SQL`
        DELETE FROM oauth_state
        WHERE state = ${state}
        RETURNING user_id, team_id, code_verifier, expires_at
      `);

      if (result.rows.length === 0) {
        return null;
      }

      const row = result.rows[0];
      const expiresAt = new Date(row.expires_at).getTime();

      // Check if expired
      if (expiresAt < Date.now()) {
        return null;
      }

      return {
        userId: row.user_id,
        teamId: row.team_id,
        codeVerifier: row.code_verifier,
      };
    } catch (error) {
      this.logger.error(error, `Failed to consume OAuth state: ${error}`);
      throw new Error(`Failed to consume OAuth state: ${error}`);
    }
  }

  /**
   * Delete OAuth state
   */
  private async delete(state: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        DELETE FROM oauth_state
        WHERE state = ${state}
      `);
      this.logger.debug(`Deleted OAuth state ${state}`);
    } catch (error) {
      this.logger.error(error, `Failed to delete OAuth state: ${error}`);
      // Don't throw - deletion failure shouldn't break the flow
    }
  }
}
