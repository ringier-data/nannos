import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';
import type { AdminSession, IAdminSessionStore } from '../../types.js';

export class PgAdminSessionStore implements IAdminSessionStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgAdminSessionStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  async createSession(session: AdminSession): Promise<void> {
    try {
      await this.pool.query(SQL`
        INSERT INTO admin_sessions (session_id, sub, email, groups, access_token, refresh_token, access_token_expires_at, created_at, expires_at)
        VALUES (
          ${session.sessionId},
          ${session.sub},
          ${session.email},
          ${session.groups},
          ${session.accessToken},
          ${session.refreshToken},
          ${new Date(session.accessTokenExpiresAt)},
          ${new Date(session.createdAt)},
          ${new Date(session.expiresAt)}
        )
      `);
      this.logger.debug(`Created admin session for sub=${session.sub}`);
    } catch (error) {
      this.logger.error(error, `Failed to create admin session: ${error}`);
      throw new Error(`Failed to create admin session: ${error}`);
    }
  }

  async getSession(sessionId: string): Promise<AdminSession | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT session_id, sub, email, groups, access_token, refresh_token, access_token_expires_at, created_at, expires_at
        FROM admin_sessions
        WHERE session_id = ${sessionId}
      `);

      if (result.rows.length === 0) {
        return null;
      }

      const row = result.rows[0];
      const expiresAt = new Date(row.expires_at).getTime();

      if (expiresAt < Date.now()) {
        await this.deleteSession(sessionId);
        return null;
      }

      return {
        sessionId: row.session_id,
        sub: row.sub,
        email: row.email,
        groups: row.groups ?? [],
        accessToken: row.access_token,
        refreshToken: row.refresh_token,
        accessTokenExpiresAt: new Date(row.access_token_expires_at).getTime(),
        createdAt: new Date(row.created_at).getTime(),
        expiresAt,
      };
    } catch (error) {
      this.logger.error(error, `Failed to get admin session: ${error}`);
      throw new Error(`Failed to get admin session: ${error}`);
    }
  }

  async updateSession(
    sessionId: string,
    updates: { accessToken: string; refreshToken?: string; accessTokenExpiresAt: number }
  ): Promise<void> {
    try {
      await this.pool.query(SQL`
        UPDATE admin_sessions
        SET access_token = ${updates.accessToken},
            refresh_token = ${updates.refreshToken},
            access_token_expires_at = ${new Date(updates.accessTokenExpiresAt)}
        WHERE session_id = ${sessionId}
      `);
      this.logger.debug(`Updated admin session ${sessionId}`);
    } catch (error) {
      this.logger.error(error, `Failed to update admin session: ${error}`);
      throw new Error(`Failed to update admin session: ${error}`);
    }
  }

  async deleteSession(sessionId: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        DELETE FROM admin_sessions WHERE session_id = ${sessionId}
      `);
      this.logger.debug(`Deleted admin session ${sessionId}`);
    } catch (error) {
      this.logger.error(error, `Failed to delete admin session: ${error}`);
      throw new Error(`Failed to delete admin session: ${error}`);
    }
  }

  async deleteExpired(): Promise<number> {
    try {
      const result = await this.pool.query(SQL`
        DELETE FROM admin_sessions WHERE expires_at < ${new Date()}
      `);
      const count = result.rowCount ?? 0;
      if (count > 0) {
        this.logger.info(`Cleaned up ${count} expired admin sessions`);
      }
      return count;
    } catch (error) {
      this.logger.error(error, `Failed to delete expired admin sessions: ${error}`);
      throw new Error(`Failed to delete expired admin sessions: ${error}`);
    }
  }
}
