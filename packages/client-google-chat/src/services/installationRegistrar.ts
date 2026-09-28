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
import type { BrokerClient } from './brokerClient.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('InstallationRegistrar');

interface DeliveryChannelCreateBody {
  name: string;
  description?: string;
  webhook_url: string;
  secret: string;
  installation_id: string;
  /**
   * How this channel renders delivered text. The scheduler sends it to the run that
   * writes a notification, so scheduled messages arrive formatted for this client
   * without every job author having to ask for it.
   */
  message_formatting: 'google-chat';
}

export interface InstallationRegistrarDeps {
  config: Config;
  oidcClient: OIDCClient;
  installationSecretService: InstallationSecretService;
  /**
   * The token broker, in broker mode. Registration then also tells it which installation
   * each project is, which is where that project's sign-ins can be reached (ADR-0011
   * amendment 1).
   */
  broker?: BrokerClient;
  /** Waits before each retry of that publication; the default spreads them over ~12 minutes. */
  publishRetryDelaysMs?: number[];
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
        // The GCP project name identifies the tenant; bot_name is a display string two
        // projects may legitimately share, which collapsed them onto one channel and secret.
        installationId: project.projectName,
        name: `Google Chat ${project.botName} (${project.projectName})`,
        description: `Google Chat project ${project.projectName} via ${project.botName}`,
      });
    } catch (error) {
      logger.error(error, `Failed to register delivery channel for project=${project.projectName}: ${error}`);
    }
  }

  if (deps.broker) {
    // A sign-in is keyed by the project NUMBER; its channel is registered under the NAME.
    const workspaces = new Map<string, string[]>();
    for (const project of config.googleChatConfigs) {
      workspaces.set(project.projectNumber, [...(workspaces.get(project.projectNumber) ?? []), project.projectName]);
    }
    await publishWorkspaceInstallations(deps.broker, workspaces, deps.publishRetryDelaysMs);
  }
}

/** Waits before each retry of a workspace publication that failed. */
const PUBLISH_RETRY_DELAYS_MS = [30_000, 120_000, 600_000];

/**
 * Tell the broker the installations this client runs in each workspace: where every sign-in
 * there can be reached (ADR-0011 amendment 1). This is the only writer of that list, so a
 * failure is retried rather than left for a restart. Per-workspace isolation, like the
 * channels: one refusal does not hold up the rest. After the last retry the next restart
 * publishes again.
 */
export async function publishWorkspaceInstallations(
  broker: BrokerClient,
  workspaces: Map<string, string[]>,
  retryDelaysMs: number[] = PUBLISH_RETRY_DELAYS_MS
): Promise<void> {
  let pending = [...workspaces];
  for (let attempt = 0; ; attempt++) {
    const failed: [string, string[]][] = [];
    for (const [workspaceId, installationIds] of pending) {
      try {
        await broker.setWorkspaceInstallations(workspaceId, installationIds);
      } catch (error) {
        logger.warn(`Failed to publish the installations of workspace ${workspaceId}: ${error}`);
        failed.push([workspaceId, installationIds]);
      }
    }
    if (failed.length === 0) {
      return;
    }
    if (attempt >= retryDelaysMs.length) {
      logger.error(
        `Gave up publishing the installations of ${failed.map(([id]) => id).join(', ')}; the next restart publishes them again`
      );
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, retryDelaysMs[attempt]));
    pending = failed;
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
    message_formatting: 'google-chat',
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
