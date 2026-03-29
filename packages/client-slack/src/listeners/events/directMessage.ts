import { App } from '@slack/bolt';
import { UserAuthService } from '../../services/userAuthService.js';
import { A2AClientService } from '../../services/a2aClientService.js';
import { FileStorageService } from '../../services/fileStorageService.js';
import { Logger } from '../../utils/logger.js';
import { SlackFile } from '../../utils/fileUtils.js';
import type { IContextStore, IPendingRequestStore, IInFlightTaskStore, IOAuthStateStore } from '../../storage/types.js';
import { handleIncomingMessage } from './messageHandler.js';

/**
 * Register DM message listeners.
 *
 * Filters for direct-message events, normalizes them into a
 * {@link NormalizedMessage}, and delegates to the shared
 * {@link handleIncomingMessage} handler.  botToken and botName are resolved
 * per-event from Bolt's authorize context so each bot persona is handled
 * correctly.
 */
export function registerMessageListeners(
  app: App,
  userAuthService: UserAuthService,
  a2aClientService: A2AClientService,
  contextStore: IContextStore,
  pendingRequestStore: IPendingRequestStore,
  inFlightTaskStore: IInFlightTaskStore,
  _oauthStateStore: IOAuthStateStore,
  baseUrl: string,
  fileStorageService: FileStorageService,
  isLocalMode: boolean
): void {
  const logger = Logger.getLogger('registerMessageListeners');
  logger.info('Registering DM message listener');

  app.message(async ({ event, body, client, context }) => {
    const ev = event as any;

    // Only handle DMs (channel_type is 'im')
    if (ev.channel_type !== 'im') {
      return;
    }

    // Ignore bot messages (prevents infinite loops) and non-user events
    if (!ev.user || ev.bot_id) {
      return;
    }

    // Skip subtypes that don't represent new user content
    const ignoredSubtypes = new Set([
      'message_changed',
      'message_deleted',
      'channel_join',
      'channel_leave',
      'bot_message',
    ]);
    if (ev.subtype && ignoredSubtypes.has(ev.subtype)) {
      return;
    }

    const text = ev.text?.trim() || '';
    const files = ev.files as SlackFile[] | undefined;

    if (!text && (!files || files.length === 0)) {
      return;
    }

    const teamId = ev.team || (body as any).team_id || '';
    if (!teamId) {
      logger.info(`No team ID available for DM from user ${ev.user}`);
    }

    const appId = (body as any).api_app_id as string | undefined;
    const botToken = context.botToken as string;
    const botName = ((context as any).botName as string | undefined) ?? 'Bot';

    logger.info('>>> DM message received!');

    await handleIncomingMessage(
      {
        userId: ev.user,
        teamId,
        channelId: ev.channel,
        messageTs: ev.ts,
        threadTs: ev.thread_ts || ev.ts,
        rawText: text,
        files: ev.files as SlackFile[] | undefined,
        source: 'direct_message',
        client,
        appId,
      },
      {
        userAuthService,
        a2aClientService,
        contextStore,
        pendingRequestStore,
        inFlightTaskStore,
        baseUrl,
        botToken,
        botName,
        fileStorageService,
        isLocalMode,
      }
    );
  });
}
