import { App } from '@slack/bolt';
import { Logger } from '../../utils/logger.js';
import { handleIncomingMessage, HandlerDependencies, NormalizedMessage } from '../events/messageHandler.js';
import { recordDecision } from '../../utils/taskResponseHandler.js';

/**
 * Register handler for HITL "Request Changes" modal submission.
 * When user submits, we send a reject decision with their feedback so
 * the LLM can re-propose the tool call incorporating the user's instructions.
 */
export function registerHitlModalHandler(app: App, makeDeps: () => HandlerDependencies): void {
  const logger = Logger.getLogger('hitlModal');

  app.view('hitl_submit', async ({ ack, body, view, client }) => {
    await ack();

    const userId = body.user.id;
    logger.info(`HITL request-changes modal submitted by user ${userId}`);

    let privateMetadata: any;
    try {
      privateMetadata = JSON.parse(view.private_metadata);
      const { channelId, threadTs, messageTs, toolName, taskId } = privateMetadata;

      // Extract user's feedback from the form
      const feedbackBlock = view.state?.values?.feedback_block;
      const feedback = feedbackBlock?.feedback_input?.value?.trim();

      if (!feedback) {
        // No feedback provided — treat as a no-op, inform user
        logger.info(`HITL modal submitted with no feedback for task ${taskId}, ignoring`);
        if (channelId && threadTs) {
          await client.chat.postMessage({
            channel: channelId,
            thread_ts: threadTs,
            text: `ℹ️ No changes requested — use the Approve or Reject buttons on the original message.`,
          });
        }
        return;
      }

      // Send reject decision with user's feedback as the message.
      // The LLM will see this as a ToolMessage(status="error") and re-propose.
      const rejectMessage = `The user requested changes to this tool call. Please revise and try again.\n\nUser feedback: ${feedback}`;
      const decisions = { decisions: [{ type: 'reject', message: rejectMessage }] };

      const syntheticMessage: NormalizedMessage = {
        userId,
        teamId: body.team?.id || '',
        channelId,
        messageTs: messageTs || Date.now().toString(),
        threadTs,
        rawText: '',
        dataParts: [decisions],
        source: 'direct_message',
        client,
        planMessageTs: privateMetadata.planMessageTs,
        resumeStreamTs: privateMetadata.streamMessageTs,
      };

      // Record the decision — into the open thinking stream if possible.
      if (channelId && messageTs) {
        await recordDecision(
          client,
          channelId,
          messageTs,
          privateMetadata.streamMessageTs,
          'Changes requested',
          privateMetadata.summary || toolName,
          false
        );
      }

      // Fire the message to the orchestrator
      handleIncomingMessage(syntheticMessage, makeDeps()).catch((err) => {
        logger.error(err, `Failed to send HITL feedback to orchestrator: ${err}`);
      });

      logger.info(`HITL feedback sent to orchestrator for tool ${toolName}`);
    } catch (error) {
      logger.error(error, `Failed to process HITL modal submission: ${error}`);
      if (privateMetadata?.channelId && privateMetadata?.threadTs) {
        await client.chat.postMessage({
          channel: privateMetadata.channelId,
          thread_ts: privateMetadata.threadTs,
          text: `❌ Failed to process feedback: ${error instanceof Error ? error.message : 'unknown error'}`,
        });
      }
    }
  });

  /**
   * Register handler for the multi-action HITL modal. Collects one Approve/Reject
   * decision per pending call and submits them as a single batched DataPart, echoing
   * each call's id so the server aligns decisions by id (no unseen-call approvals).
   */
  app.view('hitl_multi_submit', async ({ ack, body, view, client }) => {
    await ack();

    const userId = body.user.id;
    let pm: any;
    try {
      pm = JSON.parse(view.private_metadata);
      const { channelId, threadTs, messageTs, calls } = pm;
      const callList: any[] = Array.isArray(calls) ? calls : [];

      const decisions = callList.map((c: any, idx: number) => {
        const selected = view.state?.values?.[`call_${idx}`]?.[`decision_${idx}`]?.selected_option?.value || 'approve';
        const decision: Record<string, unknown> = {};
        if (c?.id) decision.id = c.id; // echo call id → server aligns by id
        if (selected === 'reject') {
          decision.type = 'reject';
          // No message → the server supplies the default rejection text.
        } else if (selected === 'approve_bypass_tool') {
          decision.type = 'approve';
          decision.bypass = true;
          decision.bypass_all = true;
        } else if (selected === 'approve_bypass_pattern') {
          decision.type = 'approve';
          decision.bypass = true;
          decision.bypass_pattern = c?.pattern;
        } else {
          decision.type = 'approve';
        }
        return decision;
      });

      logger.info(`HITL multi-decision submitted by user ${userId}: ${decisions.length} decision(s)`);

      const syntheticMessage: NormalizedMessage = {
        userId,
        teamId: body.team?.id || '',
        channelId,
        messageTs: messageTs || Date.now().toString(),
        threadTs,
        rawText: '',
        dataParts: [{ decisions }],
        source: 'direct_message',
        client,
        planMessageTs: pm.planMessageTs,
        resumeStreamTs: pm.streamMessageTs,
      };

      // Record per-call decisions: one line per call showing approve/reject +
      // the tool and its args, folded into the open thinking stream if possible.
      if (channelId && messageTs) {
        const summary = decisions
          .map((d, idx) => {
            const c = callList[idx] || {};
            const name = c.name || 'tool';
            const detail = c.detail ? ` — ${c.detail}` : '';
            const bypass = d.bypass_all ? ' (always allow)' : d.bypass_pattern ? ' (pattern allowed)' : '';
            return `${d.type === 'reject' ? '🚫' : '✅'} ${name}${detail}${bypass}`;
          })
          .join('\n');
        const anyApproved = decisions.some((d) => d.type !== 'reject');
        await recordDecision(client, channelId, messageTs, pm.streamMessageTs, 'Decisions', summary, anyApproved);
      }

      handleIncomingMessage(syntheticMessage, makeDeps()).catch((err) => {
        logger.error(err, `Failed to send HITL multi-decisions to orchestrator: ${err}`);
      });
    } catch (error) {
      logger.error(error, `Failed to process HITL multi-modal submission: ${error}`);
      if (pm?.channelId && pm?.threadTs) {
        await client.chat.postMessage({
          channel: pm.channelId,
          thread_ts: pm.threadTs,
          text: `❌ Failed to process decisions: ${error instanceof Error ? error.message : 'unknown error'}`,
        });
      }
    }
  });

  logger.info('Registered HITL modal handler');
}
