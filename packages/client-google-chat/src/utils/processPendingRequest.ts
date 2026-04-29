import { Logger } from './logger.js';
import type { PendingRequest } from '../storage/types.js';
import { handleIncomingMessage, type NormalizedMessage } from '../handlers/messageHandler.js';
import { GoogleChatService } from '../services/googleChatService.js';
import { HandlerDependencies } from '../handlers/types.js';
import type { GoogleChatAttachment } from './fileUtils.js';

const logger = Logger.getLogger('processPendingRequest');

/**
 * Process a pending request after user authorization.
 *
 * Re-fetches the original message (to recover files/attachments),
 * builds a NormalizedMessage, and delegates to handleIncomingMessage
 * so the handling is identical to a live event.
 */
export async function processPendingRequest(
  pendingRequest: PendingRequest,
  chatService: GoogleChatService,
  deps: HandlerDependencies
): Promise<void> {
  const { visitorId, spaceId, threadId, messageId, source, userEmail } = pendingRequest;
  const [projectId, userId] = visitorId.split(':');

  logger.info(`Processing pending request for user ${userId}: "${pendingRequest.text.substring(0, 50)}..."`);

  try {
    // Notify user that auth succeeded and we're picking up their request
    await chatService.sendTextMessage(
      projectId,
      spaceId,
      '✅ Authorization successful! Now processing your request...',
      threadId
    );

    // Retrieve the original message to recover any file attachments
    let attachments: GoogleChatAttachment[] | undefined;
    try {
      const messages = await chatService.listMessages(projectId, spaceId, threadId);
      const originalMessage = messages.find((m) => m.name === messageId);
      if (originalMessage?.attachment?.length) {
        attachments = originalMessage.attachment
          .filter((a) => a.source === 'UPLOADED_CONTENT' && a.attachmentDataRef?.resourceName)
          .map((a) => ({
            name: a.name ?? '',
            contentName: a.contentName ?? '',
            contentType: a.contentType ?? 'application/octet-stream',
            attachmentDataRef: a.attachmentDataRef
              ? { resourceName: a.attachmentDataRef.resourceName ?? '' }
              : undefined,
            source: 'UPLOADED_CONTENT',
          }));
        logger.info(`Recovered ${attachments.length} attachment(s) from original message`);
      }
    } catch (err) {
      logger.warn(`Could not retrieve original message attachments: ${err}`);
    }

    // Build the NormalizedMessage from stored data
    const normalizedMsg: NormalizedMessage = {
      userId,
      projectId,
      spaceId,
      messageId,
      threadId,
      rawText: pendingRequest.text,
      attachments,
      source,
      userEmail,
    };

    await handleIncomingMessage(normalizedMsg, deps);

    logger.info(`Successfully processed pending request for user ${userId}`);
  } catch (error) {
    logger.error(error, `Error processing pending request: ${error}`);
    try {
      await chatService.sendTextMessage(
        projectId,
        spaceId,
        '❌ An error occurred while processing your request. Please try again.',
        threadId
      );
    } catch (e) {
      logger.error(`Failed to send error message: ${e}`);
    }
  }
}
