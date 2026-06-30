import { Pool } from 'pg';
import { SQL } from 'sql-template-strings';
import { Logger } from '../../../utils/logger.js';
import type { IInstallationSecretStore } from '../../types.js';

/**
 * PostgreSQL storage layer for per-installation notification secrets.
 * Backs the 'db' installation-secret provider — the cloud-agnostic default
 * that requires no object store or external secret manager.
 */
export class PgInstallationSecretStore implements IInstallationSecretStore {
  private readonly pool: Pool;
  private readonly logger = Logger.getLogger(PgInstallationSecretStore.name);

  constructor(pool: Pool) {
    this.pool = pool;
  }

  async get(installationId: string): Promise<string | null> {
    try {
      const result = await this.pool.query(SQL`
        SELECT secret FROM installation_secrets WHERE installation_id = ${installationId}
      `);
      return result.rows.length > 0 ? result.rows[0].secret : null;
    } catch (error) {
      this.logger.error(error, `Failed to get installation secret for ${installationId}: ${error}`);
      throw new Error(`Failed to retrieve installation secret: ${error}`);
    }
  }

  async insertIfAbsent(installationId: string, candidate: string): Promise<string> {
    try {
      // INSERT ... ON CONFLICT DO NOTHING is atomic: only one concurrent caller
      // wins the insert. RETURNING yields a row only on a successful insert, so a
      // loser re-reads the winner's value, letting all replicas converge.
      const inserted = await this.pool.query(SQL`
        INSERT INTO installation_secrets (installation_id, secret)
        VALUES (${installationId}, ${candidate})
        ON CONFLICT (installation_id) DO NOTHING
        RETURNING secret
      `);
      if (inserted.rows.length > 0) {
        this.logger.info(`Generated and stored notification secret for installation_id=${installationId}`);
        return inserted.rows[0].secret;
      }

      const existing = await this.get(installationId);
      if (existing) {
        this.logger.info(`Notification secret already provisioned by another replica for installation_id=${installationId}`);
        return existing;
      }
      // Should be unreachable: the conflict implies a row exists.
      throw new Error(`Installation secret for ${installationId} vanished after conflict`);
    } catch (error) {
      this.logger.error(error, `Failed to insert installation secret for ${installationId}: ${error}`);
      throw new Error(`Failed to persist installation secret: ${error}`);
    }
  }
}
