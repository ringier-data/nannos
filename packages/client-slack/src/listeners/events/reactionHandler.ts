import type { App } from '@slack/bolt';
import { Logger } from '../../utils/logger.js';
import { FeedbackService } from '../../services/feedbackService.js';

const logger = Logger.getLogger('reactionHandler');

/**
 * Map Slack reaction emoji names to feedback ratings.
 * Standard reaction names (without skin-tone modifiers).
 */
const REACTION_MAP: Record<string, 'positive' | 'negative'> = {
  '+1': 'positive',
  thumbsup: 'positive',
  '-1': 'negative',
  thumbsdown: 'negative',
};

/**
 * Register listeners for `reaction_added` and `reaction_removed` events.
 *
 * When a user adds 👍 or 👎 to a bot response message, we look up the
 * corresponding A2A context/task IDs from the in-memory response mapping
 * cache and submit (or delete) feedback via the console-backend API.
 */
export function registerReactionListeners(
  app: App,
  feedbackService: FeedbackService,
): void {
  app.event('reaction_added', async ({ event }) => {
    logger.debug(`Reaction event received: emoji=${event.reaction}, user=${event.user}, item.type=${event.item.type}, channel=${event.item.channel}, ts=${event.item.ts}`);
    
    const reaction = event.reaction.replace(/::skin-tone-\d/g, '');
    const rating = REACTION_MAP[reaction];
    if (!rating) {
      logger.debug(`Ignoring reaction "${event.reaction}" - not a feedback emoji`);
      return; // Not a feedback reaction
    }

    if (event.item.type !== 'message') {
      logger.debug(`Ignoring reaction - item type is "${event.item.type}", not "message"`);
      return;
    }

    const channelId = event.item.channel;
    const ts = event.item.ts;
    const userId = event.user;

    logger.debug(`Looking up response mapping for: channelId=${channelId}, ts=${ts}`);
    const mapping = feedbackService.responseMapping.get(channelId, ts);
    if (!mapping) {
      logger.warn(`No response mapping found for reaction on ${channelId}:${ts} - feedback cannot be submitted`);
      logger.debug(`(Mapping is only stored for messages posted by this bot after an A2A response. Check if the message was posted by the Slack app.)`);
      return;
    }

    logger.info(`Reaction ${reaction} (${rating}) by ${userId} on ${channelId}:${ts}`);

    await feedbackService.submitFeedback(
      mapping.userId,
      mapping.teamId,
      mapping.contextId,
      mapping.taskId,
      rating,
    );
  });

  app.event('reaction_removed', async ({ event }) => {
    const reaction = event.reaction.replace(/::skin-tone-\d/g, '');
    if (!REACTION_MAP[reaction]) return;

    if (event.item.type !== 'message') return;

    const channelId = event.item.channel;
    const ts = event.item.ts;

    const mapping = feedbackService.responseMapping.get(channelId, ts);
    if (!mapping) return;

    logger.info(`Reaction ${reaction} removed by ${event.user} on ${channelId}:${ts}`);

    await feedbackService.deleteFeedback(
      mapping.userId,
      mapping.teamId,
      mapping.contextId,
      mapping.taskId,
    );
  });

  logger.info('Registered reaction listeners for message feedback');
}
