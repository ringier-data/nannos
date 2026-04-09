import { ParameterizedContext } from 'koa';
import type { Config } from '../config/config.js';

// Slack App manifest template with mustache-style placeholders
const MANIFEST_TEMPLATE = {
  _metadata: { major_version: 1, minor_version: 1 },
  display_information: {
    name: '{{botName}}',
    description: '{{botDescription}}',
    background_color: '#4A154B',
    long_description:
      'The Nannos Bot allows you to interact with Ringier services directly from Slack. Mention @nannos to create tasks, tickets, and artifacts based on your conversations. This uses the Nannos Agents A2A framework.',
  },
  features: {
    app_home: {
      home_tab_enabled: false,
      messages_tab_enabled: true,
      messages_tab_read_only_enabled: false,
    },
    bot_user: {
      display_name: '{{botName}}',
      always_online: true,
    },
    slash_commands: [
      {
        command: '{{slashCommand}}',
        description: 'Nannos commands: login, debug, help',
        usage_hint: '<login|debug|help> [args]',
        url: 'https://{{baseDomain}}/api/v1/slack/events',
        should_escape: false,
      },
    ],
  },
  oauth_config: {
    scopes: {
      bot: [
        'app_mentions:read',
        'channels:history',
        'channels:read',
        'chat:write',
        'chat:write.public',
        'chat:write.customize',
        'commands',
        'files:read',
        'files:write',
        'groups:history',
        'im:history',
        'im:read',
        'im:write',
        'reactions:read',
        'reactions:write',
        'users:read',
        'users:read.email',
      ],
    },
  },
  settings: {
    event_subscriptions: {
      request_url: 'https://{{baseDomain}}/api/v1/slack/events',
      bot_events: ['app_mention', 'message.im'],
    },
    interactivity: {
      is_enabled: true,
      request_url: 'https://{{baseDomain}}/api/v1/slack/events',
    },
    org_deploy_enabled: false,
    socket_mode_enabled: false,
    token_rotation_enabled: false,
  },
};

export class ConfigController {
  private readonly baseDomain: string;

  constructor(config: Config) {
    // Extract domain from baseUrl (e.g. "https://a2a-slack.d.nannos.ringier.ch" -> "a2a-slack.d.nannos.ringier.ch")
    this.baseDomain = new URL(config.baseUrl).hostname;
  }

  /** GET /api/v2/config — public configuration needed by the frontend */
  async getConfig(ctx: ParameterizedContext) {
    ctx.body = { baseDomain: this.baseDomain };
  }

  /** POST /api/v2/installations/manifest — generate a Slack App manifest with substituted values */
  async generateManifest(ctx: ParameterizedContext) {
    const { botName, slashCommand, botDescription, socketMode } = ctx.request.body as {
      botName: string;
      slashCommand: string;
      botDescription?: string;
      socketMode?: boolean;
    };

    const description = botDescription ?? `${botName} — A2A Slack bot powered by Nannos`;

    // Deep-clone the template and substitute placeholders
    const manifest = JSON.parse(
      JSON.stringify(MANIFEST_TEMPLATE)
        .replaceAll('{{botName}}', botName)
        .replaceAll('{{slashCommand}}', slashCommand)
        .replaceAll('{{baseDomain}}', this.baseDomain)
        .replaceAll('{{botDescription}}', description)
    );

    // Socket mode: strip webhook URLs and enable socket_mode
    if (socketMode) {
      manifest.settings.socket_mode_enabled = true;
      delete manifest.settings.event_subscriptions.request_url;
      delete manifest.settings.interactivity.request_url;
      delete manifest.features.slash_commands[0].url;
    }

    ctx.body = { manifest };
  }
}
