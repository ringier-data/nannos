import { App } from '@slack/bolt';
import { UserAuthService } from '../../services/userAuthService.js';
import { A2AClientService } from '../../services/a2aClientService.js';
import { FileStorageService } from '../../services/fileStorageService.js';
import type { IContextStore, IPendingRequestStore, IInFlightTaskStore, IOAuthStateStore } from '../../storage/types.js';
import { Logger } from '../../utils/logger.js';
import { SlackFile } from '../../utils/fileUtils.js';
import { handleIncomingMessage } from './messageHandler.js';
import { FeedbackService } from '../../services/feedbackService.js';

/**
 * Register app mention listener.
 *
 * Normalizes the Slack app_mention event and delegates to the shared
 * {@link handleIncomingMessage} handler.  botToken and botName are resolved
 * per-event from Bolt's authorize context so each bot persona is handled
 * correctly.
 */
export function registerAppMentionListener(
  app: App,
  userAuthService: UserAuthService,
  a2aClientService: A2AClientService,
  contextStore: IContextStore,
  pendingRequestStore: IPendingRequestStore,
  inFlightTaskStore: IInFlightTaskStore,
  _oauthStateStore: IOAuthStateStore,
  baseUrl: string,
  fileStorageService: FileStorageService,
  isLocalMode: boolean,
  feedbackService?: FeedbackService,
): void {
  const logger = Logger.getLogger('registerAppMentionListener');
  logger.info('Registering app_mention event listener');

  app.event('app_mention', async ({ event, body, client, context }) => {
    logger.info('>>> app_mention event received!');

    const userId = event.user;
    if (!userId) {
      logger.debug('App mention event has no user ID, ignoring.');
      return;
    }

    const teamId = event.team || (body as any).team_id || '';
    const appId = (body as any).api_app_id as string | undefined;
    const botToken = context.botToken as string;
    const botName = ((context as any).botName as string | undefined) ?? 'Bot';

    await handleIncomingMessage(
      {
        userId,
        teamId,
        channelId: event.channel,
        messageTs: event.ts,
        threadTs: event.thread_ts || event.ts,
        rawText: event.text,
        files: (event as any).files as SlackFile[] | undefined,
        source: 'app_mention',
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
        feedbackService,
      }
    );
  });
}
