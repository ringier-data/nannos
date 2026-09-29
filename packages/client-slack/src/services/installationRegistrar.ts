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
import { IBotInstallationStore } from '../storage/types.js';
import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('InstallationRegistrar');

interface DeliveryChannelCreateBody {
  name: string;
  description?: string;
  webhook_url: string;
  secret: string;
  installation_id: string;
  /**
   * The Slack team the app is installed in: the workspace a brokered sign-in is bound to.
   * A sign-in in that team reaches this channel (ADR-0011 amendment 2).
   */
  workspace_id: string;
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
        workspaceId: bot.teamId,
        name: `Slack ${bot.botName} (${bot.teamId})`,
        description: `Slack workspace ${bot.teamId} via ${bot.botName} (${bot.slashCommand})`,
      });
    } catch (error) {
      // Per-installation isolation — keep going.
      logger.error(error, `Failed to register delivery channel for appId=${bot.appId}: ${error}`);
    }
  }

  const retired = new Set<string>(installations.filter((b) => !b.isActive).map((b) => b.appId));
  if (retired.size > 0) {
    await clearRetiredWorkspaces(deps, retired);
  }
}

/**
 * A deactivated app keeps its channel, and with it the team it was registered in, so a
 * sign-in in that team would still read as reaching it while its webhook refuses every
 * push. Clear the team of each such channel. Update only: a channel an admin deleted, or
 * one that was never created, must not come back on every boot.
 */
async function clearRetiredWorkspaces(deps: InstallationRegistrarDeps, appIds: Set<string>): Promise<void> {
  const { config, oidcClient } = deps;
  if (!config.consoleBackend) return;
  try {
    const token = await oidcClient.getServiceToken(config.consoleBackend.audience);
    const base = config.consoleBackend.url;
    const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` };
    // With this client's own token the list holds only its channels.
    const listed = await fetch(new URL('/api/v1/delivery-channels', base).toString(), { headers });
    if (!listed.ok) {
      throw new Error(`listing delivery channels returned ${listed.status}`);
    }
    const { channels } = (await listed.json()) as {
      channels: { id: number; installation_id: string | null; workspace_id: string | null }[];
    };
    for (const channel of channels) {
      if (!channel.installation_id || !appIds.has(channel.installation_id) || channel.workspace_id === null) {
        continue;
      }
      try {
        const response = await fetch(new URL(`/api/v1/delivery-channels/${channel.id}`, base).toString(), {
          method: 'PATCH',
          headers,
          body: JSON.stringify({ workspace_id: null }),
        });
        if (!response.ok) {
          throw new Error(`Console-backend returned ${response.status}`);
        }
        logger.info(`Cleared the workspace of deactivated installation_id=${channel.installation_id}`);
      } catch (error) {
        logger.error(error, `Failed to clear the workspace of appId=${channel.installation_id}: ${error}`);
      }
    }
  } catch (error) {
    logger.error(error, `Could not clear the workspaces of deactivated apps: ${error}`);
  }
}

export async function registerOne(
  deps: InstallationRegistrarDeps,
  opts: { installationId: string; workspaceId: string; name: string; description?: string }
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
    workspace_id: opts.workspaceId,
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
