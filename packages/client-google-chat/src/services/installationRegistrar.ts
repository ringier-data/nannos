/**
 * InstallationRegistrar
 * ---------------------
 * Self-registers each tenant (Google Chat project) as a delivery channel with
 * console-backend on startup. Idempotent: each registration is keyed by a
 * deterministic `installation_id` so repeated boots never create duplicates.
 *
 * Authentication: server-to-server OAuth2 client_credentials grant against
 * Keycloak;
 *
 * Failures are logged but never thrown — bot startup must not depend on
 * console-backend availability.
 */

import { Config } from '../config/config.js';
import { OIDCClient } from './oidcClient.js';
import { InstallationSecretService } from './installationSecretService.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('InstallationRegistrar');

interface DeliveryChannelCreateBody {
  name: string;
  description?: string;
  webhook_url: string;
  secret: string;
  installation_id: string;
}

export interface InstallationRegistrarDeps {
  config: Config;
  oidcClient: OIDCClient;
  installationSecretService: InstallationSecretService;
}

export async function registerInstallations(deps: InstallationRegistrarDeps): Promise<void> {
  const { config } = deps;

  if (!config.consoleBackend) {
    logger.info('CONSOLE_BACKEND_URL not set — skipping delivery-channel self-registration');
    return;
  }

  if (config.googleChatConfigs.length === 0) {
    logger.info('No Google Chat projects configured — nothing to register');
    return;
  }

  for (const project of config.googleChatConfigs) {
    try {
      await registerOne(deps, {
        installationId: project.botName,
        name: `Google Chat ${project.botName} (${project.projectName})`,
        description: `Google Chat project ${project.projectName} via ${project.botName}`,
      });
    } catch (error) {
      logger.error(error, `Failed to register delivery channel for project=${project.projectName}: ${error}`);
    }
  }
}

export async function registerOne(
  deps: InstallationRegistrarDeps,
  opts: { installationId: string; name: string; description?: string }
): Promise<void> {
  const { config, oidcClient, installationSecretService } = deps;
  if (!config.consoleBackend) return;

  const secret = await installationSecretService.getOrCreate(opts.installationId);
  const token = await oidcClient.getServiceToken(config.consoleBackend.audience);
  const webhookUrl = new URL('/api/v1/a2a/callback', config.baseUrl).toString();

  const body: DeliveryChannelCreateBody = {
    name: opts.name,
    description: opts.description,
    webhook_url: webhookUrl,
    secret,
    installation_id: opts.installationId,
  };

  const url = new URL('/api/v1/delivery-channels', config.consoleBackend!.url).toString();
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(`Console-backend returned ${response.status} ${response.statusText}: ${text}`);
  }

  const created = response.status === 201;
  logger.info(`Delivery channel ${created ? 'created' : 'updated'} for installation_id=${opts.installationId}`);
}
