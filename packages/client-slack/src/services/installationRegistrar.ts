/**
 * InstallationRegistrar
 * ---------------------
 * Self-registers each tenant (Slack workspace) as a delivery channel with
 * console-backend on startup. Idempotent: each registration is keyed by a
 * deterministic `installation_id` so repeated boots never create duplicates.
 *
 * Authentication: server-to-server OAuth2 client_credentials grant against
 * Keycloak; the `azp` claim of the issued token becomes the channel's
 * owner (`client_id` column in delivery_channels).
 *
 * Failures are logged but never thrown — bot startup must not depend on
 * console-backend availability.
 */

import { Config } from '../config/config.js';
import { OIDCClient } from './oidcClient.js';
import { InstallationSecretService } from './installationSecretService.js';
import { BotInstallation, IBotInstallationStore } from '../storage/types.js';
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
  message_formatting: 'slack';
}

export interface InstallationRegistrarDeps {
  config: Config;
  oidcClient: OIDCClient;
  botInstallationStore: IBotInstallationStore;
  installationSecretService: InstallationSecretService;
  /**
   * The token broker, in broker mode. Registration then also tells it which apps are active
   * in each team, which is where that team's sign-ins can be reached (ADR-0011 amendment 1).
   */
  broker?: BrokerClient;
  /** Waits before each retry of that publication; the default spreads them over ~12 minutes. */
  publishRetryDelaysMs?: number[];
}

export async function registerInstallations(deps: InstallationRegistrarDeps): Promise<void> {
  const { config, botInstallationStore } = deps;

  if (!config.consoleBackend) {
    logger.info('CONSOLE_BACKEND_URL not set — skipping delivery-channel self-registration');
    return;
  }

  let installations;
  try {
    installations = await botInstallationStore.listAll();
  } catch (error) {
    logger.error(error, `Failed to list bot installations: ${error}`);
    return;
  }

  const active = installations.filter((b) => b.isActive);
  if (active.length === 0) {
    logger.info('No active bot installations — no delivery channel to register');
  }

  for (const bot of active) {
    try {
      await registerOne(deps, {
        // app_id, not bot_name: the Slack App ID is this table's primary key, is unique per
        // workspace install, and is already what every other runtime path routes on. bot_name
        // is a display string — two workspaces may legitimately both call their bot "Nannos",
        // and keying on it silently collapsed them onto one channel and one secret.
        installationId: bot.appId,
        name: `Slack ${bot.botName} (${bot.teamId})`,
        description: `Slack workspace ${bot.teamId} via ${bot.botName} (${bot.slashCommand})`,
      });
    } catch (error) {
      // Per-installation isolation — keep going.
      logger.error(error, `Failed to register delivery channel for appId=${bot.appId}: ${error}`);
    }
  }

  if (deps.broker) {
    await publishWorkspaceInstallations(deps.broker, workspacesOf(installations), deps.publishRetryDelaysMs);
  }
}

/**
 * Every team this client knows, with the apps active in it. A sign-in is per team and a
 * notification is looked up by team, so each of them reaches it. A team whose apps were all
 * deactivated maps to none, so its sign-ins stop counting as reachable.
 */
function workspacesOf(installations: BotInstallation[]): Map<string, string[]> {
  const workspaces = new Map<string, string[]>();
  for (const bot of installations) {
    const appIds = workspaces.get(bot.teamId) ?? [];
    workspaces.set(bot.teamId, bot.isActive ? [...appIds, bot.appId] : appIds);
  }
  return workspaces;
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
    message_formatting: 'slack',
  };

  const url = new URL('/api/v1/delivery-channels', config.consoleBackend.url).toString();
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
