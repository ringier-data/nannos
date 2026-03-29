import { App } from '@slack/bolt';
import { Logger } from '../../utils/logger.js';

/**
 * Register handler for the authorize button click.
 * This is needed to acknowledge the button interaction within 3 seconds,
 * even though the button uses a URL (which opens in browser).
 * Without this handler, Slack shows "operation timeout" warning.
 *
 * We also use this to delete the ephemeral authorization message when clicked.
 */
export function registerAuthorizeButtonAction(app: App): void {
  const logger = Logger.getLogger('authorizeButton');

  app.action('authorize_button', async ({ ack, respond }) => {
    // Acknowledge the action first
    await ack();
    logger.debug('Acknowledged authorize_button click');

    // Delete the ephemeral authorization message
    try {
      await respond({
        response_type: 'ephemeral',
        text: '',
        replace_original: true,
        delete_original: true,
      });
      logger.debug('Deleted ephemeral authorization message');
    } catch (error) {
      logger.debug(`Could not delete ephemeral message: ${error}`);
    }
  });
}
