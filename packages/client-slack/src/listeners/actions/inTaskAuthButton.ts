import { App } from '@slack/bolt';
import { Logger } from '../../utils/logger.js';
import { handleIncomingMessage, HandlerDependencies, NormalizedMessage } from '../events/messageHandler.js';
import { recordDecision } from '../../utils/taskResponseHandler.js';
import {
  AUTH_ACTION_DECLINE,
  AUTH_ACTION_DONE,
  AUTH_ACTION_OPEN,
  authResumeText,
  authorizationDataPart,
} from '../../utils/inTaskAuth.js';

/**
 * Register handlers for the in-task authorization card
 * (`urn:nannos:a2a:in-task-auth:1.0`).
 *
 * Both answering buttons resume the SAME parked interrupt, and both answer it
 * explicitly: the decision rides a DataPart the orchestrator's middleware acts
 * on directly — approved retries the blocked tool (so a credential that still
 * is not there asks again, as a card), declined tells the agent to stop pushing
 * the link. Agent-facing prose rides along for a server that never routed the
 * DataPart.
 *
 * Nothing is lost by a misclick: the decision reached the agent, and asking
 * again re-runs the tool and raises a fresh card.
 */
export function registerInTaskAuthActions(app: App, makeDeps: () => HandlerDependencies): void {
  const logger = Logger.getLogger('inTaskAuthButton');

  // The "Authorize" button is a URL button — Slack still delivers the click, and
  // it needs an ack within 3 seconds or the user sees "operation timeout". The
  // card is deliberately left in place: the user has to come back and confirm.
  app.action(AUTH_ACTION_OPEN, async ({ ack }) => {
    await ack();
    logger.debug('Acknowledged in-task authorize link click');
  });

  const answer = (actionId: string, decision: 'approved' | 'declined') => {
    app.action(actionId, async ({ ack, body, client }) => {
      await ack();

      const userId = body.user?.id;
      const action = (body as any).actions?.[0];
      const actionValue = action?.value || '';
      const channelId = (body as any).channel?.id;
      const messageTs = (body as any).message?.ts;
      const threadTs = (body as any).message?.thread_ts || messageTs;

      if (!actionValue || !userId || !channelId || !messageTs) {
        logger.warn(`Missing required values in ${actionId} action`);
        return;
      }

      try {
        const decoded = JSON.parse(Buffer.from(actionValue, 'base64').toString());
        const { taskId, tool, subject } = decoded;

        logger.info(`In-task auth ${decision} by user ${userId} for task ${taskId}${tool ? ` tool ${tool}` : ''}`);

        // Strip the buttons from the card (keep the trace); the resume posts the
        // outcome.
        await recordDecision(
          client,
          channelId,
          messageTs,
          decoded.streamMessageTs,
          decision === 'approved' ? 'Authorized' : 'Authorization declined',
          subject || tool || undefined,
          decision === 'approved'
        );

        const syntheticMessage: NormalizedMessage = {
          userId,
          teamId: (body as any).team?.id || '',
          channelId,
          messageTs: messageTs || Date.now().toString(),
          threadTs,
          // The text is what a server that never routed the DataPart reads; the
          // DataPart is what the middleware acts on when it did.
          rawText: authResumeText(decision, tool),
          dataParts: [authorizationDataPart(decision)],
          source: 'direct_message',
          client,
          planMessageTs: decoded.planMessageTs,
          resumeStreamTs: decoded.streamMessageTs,
        };

        handleIncomingMessage(syntheticMessage, makeDeps()).catch((err) => {
          logger.error(err, `Failed to send in-task auth ${decision} to orchestrator: ${err}`);
        });
      } catch (error) {
        logger.error(error, `Failed to process ${actionId}: ${error}`);
      }
    });
  };

  answer(AUTH_ACTION_DONE, 'approved');
  answer(AUTH_ACTION_DECLINE, 'declined');

  logger.info('Registered in-task authorization action handlers');
}
