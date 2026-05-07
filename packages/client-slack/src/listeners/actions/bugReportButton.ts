import { App } from '@slack/bolt';
import { Logger } from '../../utils/logger.js';
import { handleIncomingMessage, HandlerDependencies, NormalizedMessage } from '../events/messageHandler.js';

/**
 * Register handlers for bug report widget interactions.
 * Users can confirm/decline bug reports and optionally provide additional details.
 *
 * Button values encode taskId, contextId, reason as base64 JSON
 * to pass context through Slack's action flow.
 */
export function registerBugReportActions(app: App, makeDeps: () => HandlerDependencies): void {
  const logger = Logger.getLogger('bugReportButton');

  /**
   * Handle "Decline" button - send reject decision to orchestrator
   */
  app.action('bug_report_decline', async ({ ack, body, client }) => {
    await ack();
    
    const userId = body.user?.id;
    const action = (body as any).actions?.[0];
    const actionValue = action?.value || '';
    const channelId = (body as any).channel?.id;
    const messageTs = (body as any).message?.ts;
    const threadTs = (body as any).message?.thread_ts || messageTs;

    if (!actionValue || !userId || !channelId || !messageTs) {
      logger.warn(`Missing required values in bug_report_decline action`);
      return;
    }

    try {
      const decodedValue = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { taskId } = decodedValue;

      logger.info(`Bug report declined by user ${userId} for task ${taskId}`);

      // Remove the interactive widget (orchestrator will post the outcome)
      await client.chat.delete({
        channel: channelId,
        ts: messageTs,
      });

      // Send reject decision to orchestrator via handleIncomingMessage
      const decisions = { decisions: [{ type: 'reject', message: 'User declined' }] };
      const syntheticMessage: NormalizedMessage = {
        userId,
        teamId: (body as any).team?.id || '',
        channelId,
        messageTs: messageTs || Date.now().toString(),
        threadTs,
        rawText: '',
        dataParts: [decisions],
        source: 'direct_message',
        client,
      };

      handleIncomingMessage(syntheticMessage, makeDeps()).catch((err) => {
        logger.error(err, `Failed to send bug report decline to orchestrator: ${err}`);
      });
    } catch (error) {
      logger.error(error, `Failed to process bug_report_decline: ${error}`);
    }
  });

  /**
   * Handle "Confirm" button - open modal for optional description
   */
  app.action('bug_report_confirm', async ({ ack, body, client }) => {
    await ack();
    const logger = Logger.getLogger('bugReportButton');

    const userId = body.user?.id;
    const action = (body as any).actions?.[0];
    const actionValue = action?.value || '';
    const triggerId = (body as any).trigger_id;
    const messageTs = (body as any).message?.ts;

    if (!actionValue || !userId || !triggerId) {
      logger.warn(`Missing required values in bug_report_confirm action`);
      return;
    }

    try {
      const decodedValue = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { taskId, contextId, reason, channelId, threadTs, actionRequests } = decodedValue;

      logger.info(`Bug report confirm clicked by user ${userId} for task ${taskId}`);

      // Encode callback data in private_metadata
      const privateMetadata = JSON.stringify({
        taskId,
        contextId,
        reason,
        channelId,
        threadTs,
        messageTs,
        actionRequests,
      });

      // Open modal for optional description
      await client.views.open({
        trigger_id: triggerId,
        view: {
          type: 'modal',
          callback_id: 'bug_report_submit',
          private_metadata: privateMetadata,
          title: {
            type: 'plain_text',
            text: 'Bug Report',
            emoji: true,
          },
          submit: {
            type: 'plain_text',
            text: 'Submit Report',
            emoji: true,
          },
          close: {
            type: 'plain_text',
            text: 'Cancel',
            emoji: true,
          },
          blocks: [
            {
              type: 'section',
              text: {
                type: 'mrkdwn',
                text: `*Reason:* ${reason}`,
              },
            },
            {
              type: 'divider',
            },
            {
              type: 'input',
              block_id: 'description_block',
              optional: true,
              label: {
                type: 'plain_text',
                text: 'Additional details (optional)',
                emoji: true,
              },
              element: {
                type: 'plain_text_input',
                action_id: 'description_input',
                multiline: true,
                placeholder: {
                  type: 'plain_text',
                  text: 'Add extra context or details about this bug...',
                },
              },
            },
          ],
        },
      });
    } catch (error) {
      logger.error(error, `Failed to open bug report modal: ${error}`);
    }
  });

  logger.info('Registered bug report action handlers');
}
