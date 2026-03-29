import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { UserAuthToken } from '../../types.js';
import { Logger } from '../../../utils/logger.js';

/**
 * PostgreSQL storage layer for user authentication tokens
 */
export class PgUserAuthStorage {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgUserAuthStorage.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  /**
   * Store user authentication token
   */
  async saveToken(token: UserAuthToken): Promise<void> {
    try {
      await this.pool.query(SQL`
        INSERT INTO user_auth (
          user_id, team_id, access_token, refresh_token, expires_at,
          token_type, scope, id_token
        ) VALUES (
          ${token.userId}, ${token.teamId}, ${token.accessToken}, ${token.refreshToken},
          ${new Date(token.expiresAt)}, ${token.tokenType}, ${token.scope}, ${token.idToken}
        )
        ON CONFLICT (user_id, team_id) DO UPDATE SET
          access_token = EXCLUDED.access_token,
          refresh_token = EXCLUDED.refresh_token,
          expires_at = EXCLUDED.expires_at,
          token_type = EXCLUDED.token_type,
          scope = EXCLUDED.scope,
          id_token = EXCLUDED.id_token
      `);
      this.logger.info(`Saved auth token for user ${token.userId} in team ${token.teamId}`);
    } catch (error) {
      this.logger.error(error, `Failed to save auth token: ${error}`);
      throw new Error(`Failed to save user auth token: ${error}`);
    }
  }

  /**
   * Retrieve user authentication token
   */
  async getToken(userId: string, teamId: string): Promise<UserAuthToken | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT user_id, team_id, access_token, refresh_token, expires_at,
               token_type, scope, id_token, created_at, updated_at
        FROM user_auth
        WHERE user_id = ${userId} AND team_id = ${teamId}
      `);

      if (result.rows.length === 0) {
        return null;
      }

      const row = result.rows[0];
      return {
        userId: row.user_id,
        teamId: row.team_id,
        accessToken: row.access_token,
        refreshToken: row.refresh_token,
        expiresAt: new Date(row.expires_at).getTime(),
        tokenType: row.token_type,
        scope: row.scope,
        idToken: row.id_token,
        createdAt: new Date(row.created_at).getTime(),
        updatedAt: new Date(row.updated_at).getTime(),
      };
    } catch (error) {
      this.logger.error(error, `Failed to get auth token: ${error}`);
      throw new Error(`Failed to retrieve user auth token: ${error}`);
    }
  }

  /**
   * Delete user authentication token
   */
  async deleteToken(userId: string, teamId: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        DELETE FROM user_auth
        WHERE user_id = ${userId} AND team_id = ${teamId}
      `);
      this.logger.info(`Deleted auth token for user ${userId} in team ${teamId}`);
    } catch (error) {
      this.logger.error(error, `Failed to delete auth token: ${error}`);
      throw new Error(`Failed to delete user auth token: ${error}`);
    }
  }

  /**
   * Update token expiration and refresh token
   */
  async updateToken(
    userId: string,
    teamId: string,
    updates: {
      accessToken?: string;
      refreshToken?: string;
      expiresAt?: number;
      idToken?: string;
    }
  ): Promise<void> {
    const setClauses: string[] = [];
    const values: any[] = [];
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

    if (setClauses.length === 0) {
      return; // Nothing to update
    }

    values.push(userId, teamId);

    try {
      await this.pool.query(
        `UPDATE user_auth SET ${setClauses.join(', ')} WHERE user_id = $${paramIndex++} AND team_id = $${paramIndex}`,
        values
      );
      this.logger.info(`Updated auth token for user ${userId} in team ${teamId}`);
    } catch (error) {
      this.logger.error(error, `Failed to update auth token: ${error}`);
      throw new Error(`Failed to update user auth token: ${error}`);
    }
  }

  /**
   * Check if user has valid token
   */
  async hasValidToken(userId: string, teamId: string): Promise<boolean> {
    this.logger.info(`Checking token for userId=${userId}, teamId=${teamId}`);
    const token = await this.getToken(userId, teamId);
    if (!token) {
      this.logger.debug(`No token found for userId=${userId}, teamId=${teamId}`);
      return false;
    }

    // Check if token is expired (with 5 minute buffer)
    const now = Date.now();
    const bufferMs = 5 * 60 * 1000; // 5 minutes
    const isValid = token.expiresAt > now + bufferMs;
    this.logger.info(`Token found for userId=${userId}, teamId=${teamId}, valid=${isValid}`);
    return isValid;
  }
}
