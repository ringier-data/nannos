import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { UserAuthToken } from '../../types.js';
import { Logger } from '../../../utils/logger.js';

/**
 * Map a user_auth row. Broker rows carry no tokens, so their token columns are NULL and
 * come back as undefined.
 */
function rowToToken(row: any): UserAuthToken {
  return {
    userId: row.user_id,
    projectId: row.project_id,
    accessToken: row.access_token ?? undefined,
    refreshToken: row.refresh_token ?? undefined,
    expiresAt: row.expires_at ? new Date(row.expires_at).getTime() : undefined,
    tokenType: row.token_type ?? undefined,
    scope: row.scope ?? undefined,
    idToken: row.id_token ?? undefined,
    oidcSub: row.oidc_sub ?? undefined,
    authMode: row.auth_mode ?? 'local',
    createdAt: new Date(row.created_at).getTime(),
    updatedAt: new Date(row.updated_at).getTime(),
  };
}

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
   * Store user authentication token. A row signed in again replaces every column, so a
   * user who signs in through the broker leaves no local tokens behind.
   */
  async saveToken(token: UserAuthToken): Promise<void> {
    const expiresAt = token.expiresAt !== undefined ? new Date(token.expiresAt) : null;
    try {
      await this.pool.query(SQL`
        INSERT INTO user_auth (
          user_id, project_id, access_token, refresh_token, expires_at,
          token_type, scope, id_token, oidc_sub, auth_mode
        ) VALUES (
          ${token.userId}, ${token.projectId}, ${token.accessToken ?? null}, ${token.refreshToken ?? null},
          ${expiresAt}, ${token.tokenType ?? null}, ${token.scope ?? null}, ${token.idToken ?? null},
          ${token.oidcSub ?? null}, ${token.authMode ?? 'local'}
        )
        ON CONFLICT (user_id, project_id) DO UPDATE SET
          access_token = EXCLUDED.access_token,
          refresh_token = EXCLUDED.refresh_token,
          expires_at = EXCLUDED.expires_at,
          token_type = EXCLUDED.token_type,
          scope = EXCLUDED.scope,
          id_token = EXCLUDED.id_token,
          oidc_sub = EXCLUDED.oidc_sub,
          auth_mode = EXCLUDED.auth_mode
      `);
      this.logger.info(`Saved auth token for user ${token.userId} in project ${token.projectId}`);
    } catch (error) {
      this.logger.error(error, `Failed to save auth token: ${error}`);
      throw new Error(`Failed to save user auth token: ${error}`);
    }
  }

  /**
   * Retrieve user authentication token
   */
  async getToken(userId: string, projectId: string): Promise<UserAuthToken | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT user_id, project_id, access_token, refresh_token, expires_at,
               token_type, scope, id_token, oidc_sub, auth_mode, created_at, updated_at
        FROM user_auth
        WHERE user_id = ${userId} AND project_id = ${projectId}
      `);

      if (result.rows.length === 0) {
        return null;
      }
      return rowToToken(result.rows[0]);
    } catch (error) {
      this.logger.error(error, `Failed to get auth token: ${error}`);
      throw new Error(`Failed to retrieve user auth token: ${error}`);
    }
  }

  /**
   * Delete user authentication token
   */
  async deleteToken(userId: string, projectId: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        DELETE FROM user_auth
        WHERE user_id = ${userId} AND project_id = ${projectId}
      `);
      this.logger.info(`Deleted auth token for user ${userId} in project ${projectId}`);
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
    projectId: string,
    updates: {
      accessToken?: string;
      refreshToken?: string;
      expiresAt?: number;
      idToken?: string;
      oidcSub?: string;
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
    if (updates.oidcSub !== undefined) {
      setClauses.push(`oidc_sub = $${paramIndex++}`);
      values.push(updates.oidcSub);
    }

    if (setClauses.length === 0) {
      return; // Nothing to update
    }

    values.push(userId, projectId);

    try {
      await this.pool.query(
        `UPDATE user_auth SET ${setClauses.join(', ')} WHERE user_id = $${paramIndex++} AND project_id = $${paramIndex}`,
        values
      );
      this.logger.info(`Updated auth token for user ${userId} in project ${projectId}`);
    } catch (error) {
      this.logger.error(error, `Failed to update auth token: ${error}`);
      throw new Error(`Failed to update user auth token: ${error}`);
    }
  }

  /**
   * Check if user has a valid local access token. A broker row has none.
   */
  async hasValidToken(userId: string, projectId: string): Promise<boolean> {
    this.logger.info(`Checking token for userId=${userId}, projectId=${projectId}`);
    const token = await this.getToken(userId, projectId);
    if (!token || token.expiresAt === undefined) {
      this.logger.debug(`No local token found for userId=${userId}, projectId=${projectId}`);
      return false;
    }

    // Check if token is expired (with 5 minute buffer)
    const now = Date.now();
    const bufferMs = 5 * 60 * 1000; // 5 minutes
    const isValid = token.expiresAt > now + bufferMs;
    this.logger.info(`Token found for userId=${userId}, projectId=${projectId}, valid=${isValid}`);
    return isValid;
  }

  /**
   * Find a user auth record by OIDC subject identifier scoped to a Google Chat project.
   */
  async findByOidcSub(oidcSub: string, projectId: string): Promise<UserAuthToken | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT user_id, project_id, access_token, refresh_token, expires_at,
               token_type, scope, id_token, oidc_sub, auth_mode, created_at, updated_at
        FROM user_auth
        WHERE oidc_sub = ${oidcSub} AND project_id = ${projectId}
        LIMIT 1
      `);

      if (result.rows.length === 0) {
        return null;
      }
      return rowToToken(result.rows[0]);
    } catch (error) {
      this.logger.error(error, `Failed to find user by OIDC sub and team: ${error}`);
      throw new Error(`Failed to find user by OIDC sub and team: ${error}`);
    }
  }
}
