/**
 * AwsSsmInstallationSecretService
 * --------------------------------
 * AWS SSM Parameter Store-backed implementation of InstallationSecretService.
 * Stores secrets as SecureString parameters at `${prefix}/${sanitizedId}` and
 * generates a 32-byte hex secret on first access.
 */

import { SSMClient, GetParameterCommand, PutParameterCommand } from '@aws-sdk/client-ssm';
import { randomBytes } from 'node:crypto';
import { InstallationSecretService } from './installationSecretService.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('AwsSsmInstallationSecretService');

export class AwsSsmInstallationSecretService extends InstallationSecretService {
  constructor(
    private readonly client: SSMClient,
    private readonly prefix: string
  ) {
    super();
  }

  protected async resolve(installationId: string): Promise<string> {
    const existing = await this.read(installationId);
    if (existing) return existing;

    const name = this.parameterName(installationId);
    const generated = randomBytes(32).toString('hex');
    await this.client.send(
      new PutParameterCommand({
        Name: name,
        Value: generated,
        Type: 'SecureString',
        Overwrite: false,
        Description: `Notification secret for installation ${installationId}`,
      })
    );
    logger.info(`Generated and stored notification secret in SSM for installation_id=${installationId}`);
    return generated;
  }

  protected async read(installationId: string): Promise<string | null> {
    const name = this.parameterName(installationId);
    try {
      const res = await this.client.send(new GetParameterCommand({ Name: name, WithDecryption: true }));
      return res.Parameter?.Value ?? null;
    } catch (err) {
      if ((err as { name?: string })?.name === 'ParameterNotFound') return null;
      throw err;
    }
  }

  private parameterName(installationId: string): string {
    const sanitized = installationId.replace(/[^a-zA-Z0-9_.\-/]/g, '_');
    return `${this.prefix.replace(/\/$/, '')}/${sanitized}`;
  }
}
