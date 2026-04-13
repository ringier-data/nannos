import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';
import type { BotInstallation, IBotInstallationStore } from '../../types.js';

/**
 * PostgreSQL storage layer for bot installations.
 * Each row represents one registered Slack App persona.
 * Keyed by app_id (Slack App ID), which is the authoritative runtime lookup key.
 *
 * NOTE: Multiple bots may share the same team_id (workspace). All runtime routing
 * uses app_id, extracted from api_app_id in the Slack request body.
 */
export class PgBotInstallationStore implements IBotInstallationStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgBotInstallationStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  /**
   * Get a bot installation by Slack App ID.
   * This is the primary runtime lookup — called on every incoming Slack event
   * via the Bolt authorize callback using body.api_app_id.
   */
  async getByAppId(appId: string): Promise<BotInstallation | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT app_id, team_id, bot_token, signing_secret, bot_name, avatar_url,
               slash_command, is_active, created_at, updated_at,
               (avatar_data IS NOT NULL) AS has_avatar
        FROM bot_installations
        WHERE app_id = ${appId} AND is_active = TRUE
      `);

      if (result.rows.length === 0) {
        return null;
      }

      return this.rowToInstallation(result.rows[0]);
    } catch (error) {
      this.logger.error(error, `Failed to get bot installation by appId ${appId}: ${error}`);
      throw new Error(`Failed to retrieve bot installation: ${error}`);
    }
  }

  /**
   * Get all active bot installations for a workspace.
   * Returns an array because a workspace may have multiple personalized bots.
   */
  async getByTeamId(teamId: string): Promise<BotInstallation[]> {
    try {
      const result = await this.pool.query(SQL`
        SELECT app_id, team_id, bot_token, signing_secret, bot_name, avatar_url,
               slash_command, is_active, created_at, updated_at,
               (avatar_data IS NOT NULL) AS has_avatar
        FROM bot_installations
        WHERE team_id = ${teamId} AND is_active = TRUE
        ORDER BY created_at ASC
      `);

      return result.rows.map((row) => this.rowToInstallation(row));
    } catch (error) {
      this.logger.error(error, `Failed to get bot installations for team ${teamId}: ${error}`);
      throw new Error(`Failed to retrieve bot installations: ${error}`);
    }
  }

  /**
   * List all bot installations (active and inactive).
   * Used by the admin API.
   */
  async listAll(): Promise<BotInstallation[]> {
    try {
      const result = await this.pool.query(SQL`
        SELECT app_id, team_id, bot_token, signing_secret, bot_name, avatar_url,
               slash_command, is_active, created_at, updated_at,
               (avatar_data IS NOT NULL) AS has_avatar
        FROM bot_installations
        ORDER BY created_at ASC
      `);

      return result.rows.map((row) => this.rowToInstallation(row));
    } catch (error) {
      this.logger.error(error, `Failed to list bot installations: ${error}`);
      throw new Error(`Failed to list bot installations: ${error}`);
    }
  }

  /**
   * Create or update a bot installation.
   * On conflict (same app_id), updates credentials and metadata — but does NOT
   * overwrite bot_name or slash_command (the admin UI owns those after initial seed).
   */
  async upsert(bot: Omit<BotInstallation, 'createdAt' | 'updatedAt'>): Promise<void> {
    try {
      await this.pool.query(SQL`
        INSERT INTO bot_installations (
          app_id, team_id, bot_token, signing_secret, bot_name,
          avatar_url, slash_command, is_active
        ) VALUES (
          ${bot.appId}, ${bot.teamId}, ${bot.botToken}, ${bot.signingSecret},
          ${bot.botName}, ${bot.avatarUrl ?? null}, ${bot.slashCommand}, ${bot.isActive}
        )
        ON CONFLICT (app_id) DO UPDATE SET
          team_id        = EXCLUDED.team_id,
          bot_token      = EXCLUDED.bot_token,
          signing_secret = EXCLUDED.signing_secret,
          is_active      = EXCLUDED.is_active,
          updated_at     = NOW()
      `);
      this.logger.info(`Upserted bot installation for appId=${bot.appId} (${bot.botName})`);
    } catch (error) {
      this.logger.error(error, `Failed to upsert bot installation: ${error}`);
      throw new Error(`Failed to upsert bot installation: ${error}`);
    }
  }

  /**
   * Soft-deactivate a bot installation.
   * The row is retained for audit purposes; it is excluded from all active lookups.
   */
  async deactivate(appId: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        UPDATE bot_installations
        SET is_active = FALSE, updated_at = NOW()
        WHERE app_id = ${appId}
      `);
      this.logger.info(`Deactivated bot installation appId=${appId}`);
    } catch (error) {
      this.logger.error(error, `Failed to deactivate bot installation: ${error}`);
      throw new Error(`Failed to deactivate bot installation: ${error}`);
    }
  }

  async updateAvatar(appId: string, data: Buffer, mimeType: string): Promise<void> {
    try {
      const result = await this.pool.query(SQL`
        UPDATE bot_installations
        SET avatar_data = ${data}, avatar_mime_type = ${mimeType}, updated_at = NOW()
        WHERE app_id = ${appId}
      `);
      if (result.rowCount === 0) {
        throw new Error(`Installation not found: ${appId}`);
      }
      this.logger.info(`Updated avatar for appId=${appId} (${mimeType}, ${data.length} bytes)`);
    } catch (error) {
      this.logger.error(error, `Failed to update avatar for ${appId}: ${error}`);
      throw error;
    }
  }

  async getAvatar(appId: string): Promise<{ data: Buffer; mimeType: string } | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT avatar_data, avatar_mime_type
        FROM bot_installations
        WHERE app_id = ${appId} AND avatar_data IS NOT NULL
      `);
      if (result.rows.length === 0 || !result.rows[0].avatar_data) {
        return null;
      }
      return { data: result.rows[0].avatar_data, mimeType: result.rows[0].avatar_mime_type };
    } catch (error) {
      this.logger.error(error, `Failed to get avatar for ${appId}: ${error}`);
      throw error;
    }
  }

  async deleteAvatar(appId: string): Promise<void> {
    try {
      await this.pool.query(SQL`
        UPDATE bot_installations
        SET avatar_data = NULL, avatar_mime_type = NULL, updated_at = NOW()
        WHERE app_id = ${appId}
      `);
      this.logger.info(`Deleted avatar for appId=${appId}`);
    } catch (error) {
      this.logger.error(error, `Failed to delete avatar for ${appId}: ${error}`);
      throw error;
    }
  }

  private rowToInstallation(row: any): BotInstallation {
    return {
      appId: row.app_id,
      teamId: row.team_id,
      botToken: row.bot_token,
      signingSecret: row.signing_secret,
      botName: row.bot_name,
      avatarUrl: row.avatar_url ?? undefined,
      hasAvatar: !!row.has_avatar,
      slashCommand: row.slash_command,
      isActive: row.is_active,
      createdAt: new Date(row.created_at),
      updatedAt: new Date(row.updated_at),
    };
  }
}
