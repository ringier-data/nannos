import { Logger } from './logger.js';
import type { PendingRequest } from '../storage/types.js';
import {
  handleIncomingMessage,
  type NormalizedMessage,
  type HandlerDependencies,
} from '../listeners/events/messageHandler.js';
import type { SlackFile } from './fileUtils.js';
import type { WebClient } from '@slack/web-api';

const logger = Logger.getLogger('processPendingRequest');

/**
 * Process a pending request after user authorization.
 *
 * Re-fetches the original Slack message (to recover files/attachments),
 * builds a NormalizedMessage, and delegates to handleIncomingMessage
 * so the handling is identical to a live event.
 */
export async function processPendingRequest(
  pendingRequest: PendingRequest,
  slackClient: WebClient,
  deps: HandlerDependencies
): Promise<void> {
  const { visitorId, channelId, threadTs, messageTs, source } = pendingRequest;
  const [teamId, userId] = visitorId.split(':');

  logger.info(`Processing pending request for user ${userId}: "${pendingRequest.text.substring(0, 50)}..."`);

  try {
    // Notify user that auth succeeded and we're picking up their request
    await slackClient.chat.postMessage({
      channel: channelId,
      thread_ts: threadTs,
      text: '✅ Authorization successful! Now processing your request...',
    });

    // Re-fetch the original message from Slack API to get the full payload (including files)
    let rawText = pendingRequest.text;
    let files: SlackFile[] | undefined;

    try {
      const isInThread = threadTs !== messageTs;
      const fetchResult = isInThread
        ? await slackClient.conversations.replies({
            channel: channelId,
            ts: threadTs,
            inclusive: true,
            limit: 100,
          })
        : await slackClient.conversations.history({
            channel: channelId,
            latest: messageTs,
            inclusive: true,
            limit: 1,
          });

      const originalMsg = fetchResult.messages?.find((m) => m.ts === messageTs);

      if (originalMsg) {
        // Use the original text from Slack so mention resolution works correctly
        if (originalMsg.text) {
          rawText = originalMsg.text;
        }
        if ((originalMsg as any).files && (originalMsg as any).files.length > 0) {
          files = (originalMsg as any).files as SlackFile[];
          logger.info(`Recovered ${files.length} file(s) from original message`);
        }
      }
    } catch (err) {
      logger.warn(`Failed to re-fetch original message, proceeding with stored text: ${err}`);
    }

    // Build the same NormalizedMessage that a live event would produce
    const normalizedMsg: NormalizedMessage = {
      userId,
      teamId,
      channelId,
      messageTs,
      threadTs,
      rawText,
      files,
      source,
      client: slackClient,
    };

    await handleIncomingMessage(normalizedMsg, deps);

    logger.info(`Successfully processed pending request for user ${userId}`);
  } catch (error) {
    logger.error(error, `Error processing pending request: ${error}`);
    try {
      await slackClient.chat.postMessage({
        channel: channelId,
        thread_ts: threadTs,
        text: '❌ An error occurred while processing your request. Please try again.',
      });
    } catch (e) {
      logger.error(`Failed to send error message: ${e}`);
    }
  }
}
