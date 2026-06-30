/**
 * DbInstallationSecretService
 * ---------------------------
 * Storage-backed implementation of InstallationSecretService — the cloud-agnostic
 * default. Persists per-installation notification secrets through the application's
 * existing storage provider (e.g. PostgreSQL), so no object store or external
 * secret manager is required to run Nannos. Generates a 32-byte hex secret on
 * first access.
 */

import { randomBytes } from 'node:crypto';
import { InstallationSecretService } from './installationSecretService.js';
import type { IInstallationSecretStore } from '../storage/index.js';

export class DbInstallationSecretService extends InstallationSecretService {
  constructor(private readonly store: IInstallationSecretStore) {
    super();
  }

  protected async resolve(installationId: string): Promise<string> {
    const existing = await this.store.get(installationId);
    if (existing) return existing;

    // insertIfAbsent is race-safe: concurrent replicas converge on one secret.
    return this.store.insertIfAbsent(installationId, randomBytes(32).toString('hex'));
  }

  protected async read(installationId: string): Promise<string | null> {
    return this.store.get(installationId);
  }
}
