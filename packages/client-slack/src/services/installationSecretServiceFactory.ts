/**
 * createInstallationSecretService
 * -------------------------------
 * Selects the per-installation notification-secret backend based on config,
 * mirroring the storage-provider factory (see storage/index.ts).
 *
 *   'db'      (default) — secrets persisted via the storage provider. Cloud-agnostic;
 *                         the only option that needs no cloud account or extra infra.
 *   'aws-ssm'          — secrets in AWS SSM Parameter Store. The AWS SDK is imported
 *                         lazily so it stays an optional dependency for non-AWS deploys.
 */

import type { Config } from '../config/config.js';
import type { StorageProvider } from '../storage/index.js';
import { InstallationSecretService } from './installationSecretService.js';
import { DbInstallationSecretService } from './dbInstallationSecretService.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('InstallationSecretServiceFactory');

export async function createInstallationSecretService(
  config: Config,
  storage: StorageProvider
): Promise<InstallationSecretService> {
  const provider = config.installationSecret.provider;
  logger.info(`Creating installation secret service: provider=${provider}`);

  switch (provider) {
    case 'db':
      return new DbInstallationSecretService(storage.installationSecret);

    case 'aws-ssm': {
      // Lazy import keeps @aws-sdk/client-ssm out of the dependency graph for
      // deployments that don't use it.
      const { SSMClient } = await import('@aws-sdk/client-ssm');
      const { AwsSsmInstallationSecretService } = await import('./awsSsmInstallationSecretService.js');
      return new AwsSsmInstallationSecretService(
        new SSMClient({ region: config.aws.region }),
        config.installationSecret.ssmPrefix
      );
    }

    default:
      throw new Error(`Unknown installation secret provider: ${provider}`);
  }
}
