import { App } from '@slack/bolt';
import { Logger } from '../../utils/logger.js';
import { FeedbackService } from '../../services/feedbackService.js';

/**
 * Register handlers for feedback button clicks (thumbs up/down).
 * Users can submit positive or negative feedback on agent responses.
 *
 * Button values encode contextId, taskId, userId, teamId as base64 JSON
 * because ephemeral message block_actions don't include channel/message info.
 */
export function registerFeedbackButtonActions(app: App, feedbackService: FeedbackService): void {
  const logger = Logger.getLogger('feedbackButton');

  async function handleFeedback(
    rating: 'positive' | 'negative',
    body: any,
    respond: (msg: any) => Promise<void>,
  ): Promise<void> {
    const userId = body.user?.id;
    const actionValue = body.actions?.[0]?.value;

    if (!actionValue || !userId) {
      logger.warn(`Missing action value or user ID in feedback_${rating} action`);
      await respond({ text: '❌ Failed to process feedback (missing value). Please try again.', replace_original: true });
      return;
    }

    try {
      const decodedValue = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { contextId, taskId, userId: origUserId, teamId, subAgents } = decodedValue;

      if (!contextId || !taskId || !teamId) {
        throw new Error('Invalid encoded value: missing contextId, taskId, or teamId');
      }

      logger.info(`Feedback ${rating} from ${userId} for context=${contextId} taskId=${taskId}`);

      const subAgentId = Array.isArray(subAgents) && subAgents.length > 0 ? subAgents[0] : undefined;
      await feedbackService.submitFeedback(origUserId, teamId, contextId, taskId, rating, taskId, subAgentId);

      // Replace the widget in-place with a thank-you note (keeps thread scope and order)
      await respond({ text: '✅ Thanks for the feedback!', replace_original: true });
    } catch (error) {
      logger.error(error, `Failed to process ${rating} feedback: ${error}`);
      await respond({
        text: `❌ Failed to submit feedback: ${error instanceof Error ? error.message : 'unknown error'}`,
        replace_original: true,
      });
    }
  }

  app.action('feedback_thumbsup', async ({ ack, body, respond }) => {
    await ack();
    await handleFeedback('positive', body, respond);
  });

  app.action('feedback_thumbsdown', async ({ ack, body, respond }) => {
    await ack();
    await handleFeedback('negative', body, respond);
  });

  logger.info('Registered feedback button action handlers');
}
