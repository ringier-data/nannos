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
    const reaction = event.reaction.replace(/::skin-tone-\d/g, '');
    const rating = REACTION_MAP[reaction];
    if (!rating) return; // Not a feedback reaction

    if (event.item.type !== 'message') return;

    const channelId = event.item.channel;
    const ts = event.item.ts;
    const userId = event.user;

    const mapping = feedbackService.responseMapping.get(channelId, ts);
    if (!mapping) {
      logger.debug(`No response mapping for reaction on ${channelId}:${ts}`);
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
