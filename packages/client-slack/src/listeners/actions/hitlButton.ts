import { App } from '@slack/bolt';
import { Logger } from '../../utils/logger.js';
import { handleIncomingMessage, HandlerDependencies, NormalizedMessage } from '../events/messageHandler.js';
import { recordDecision } from '../../utils/taskResponseHandler.js';

/**
 * Register handlers for generic HITL interrupt widget interactions.
 *
 * UX flow:
 *   - "Approve" → sends approve decision directly (no modal)
 *   - "Request Changes" → opens modal showing proposed content (read-only)
 *     with a text area for natural language feedback → sends reject with that
 *     feedback so the LLM re-proposes the tool call
 *   - "Reject" → sends reject decision directly
 *
 * Button values encode taskId, contextId, toolName, reason as base64 JSON
 * to pass context through Slack's action flow.
 */
export function registerHitlActions(app: App, makeDeps: () => HandlerDependencies): void {
  const logger = Logger.getLogger('hitlButton');

  /**
   * Handle "Reject" button - send reject decision to orchestrator
   */
  app.action('hitl_reject', async ({ ack, body, client }) => {
    await ack();
    
    const userId = body.user?.id;
    const action = (body as any).actions?.[0];
    const actionValue = action?.value || '';
    const channelId = (body as any).channel?.id;
    const messageTs = (body as any).message?.ts;
    const threadTs = (body as any).message?.thread_ts || messageTs;

    if (!actionValue || !userId || !channelId || !messageTs) {
      logger.warn(`Missing required values in hitl_reject action`);
      return;
    }

    try {
      const decodedValue = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { taskId, toolName } = decodedValue;

      logger.info(`HITL rejected by user ${userId} for task ${taskId} tool ${toolName}`);

      // Strip the buttons from the interrupt block (keep the trace); the resume
      // posts the outcome.
      await recordDecision(client, channelId, messageTs, decodedValue.streamMessageTs, 'Rejected', decodedValue.summary || decodedValue.toolName, false);

      // Send reject decision to orchestrator via handleIncomingMessage.
      // No message → the server supplies the default rejection text.
      const decisions = { decisions: [{ type: 'reject' }] };
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
        planMessageTs: decodedValue.planMessageTs,
        resumeStreamTs: decodedValue.streamMessageTs,
      };

      handleIncomingMessage(syntheticMessage, makeDeps()).catch((err) => {
        logger.error(err, `Failed to send HITL reject to orchestrator: ${err}`);
      });
    } catch (error) {
      logger.error(error, `Failed to process hitl_reject: ${error}`);
    }
  });

  /**
   * Handle "Approve" button - approve directly without opening a modal.
   */
  app.action('hitl_approve', async ({ ack, body, client }) => {
    await ack();
    const logger = Logger.getLogger('hitlButton');

    const userId = body.user?.id;
    const action = (body as any).actions?.[0];
    const actionValue = action?.value || '';
    const channelId = (body as any).channel?.id;
    const messageTs = (body as any).message?.ts;
    const threadTs = (body as any).message?.thread_ts || messageTs;

    if (!actionValue || !userId || !channelId || !messageTs) {
      logger.warn(`Missing required values in hitl_approve action`);
      return;
    }

    try {
      const decodedValue = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { taskId, toolName } = decodedValue;

      logger.info(`HITL approved by user ${userId} for task ${taskId} tool ${toolName}`);

      // Strip the buttons from the interrupt block (keep the trace)
      await recordDecision(client, channelId, messageTs, decodedValue.streamMessageTs, 'Approved', decodedValue.summary || decodedValue.toolName, true);

      // Send approve decision to orchestrator
      const decisions = { decisions: [{ type: 'approve' }] };
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
        planMessageTs: decodedValue.planMessageTs,
        resumeStreamTs: decodedValue.streamMessageTs,
      };

      handleIncomingMessage(syntheticMessage, makeDeps()).catch((err) => {
        logger.error(err, `Failed to send HITL approve to orchestrator: ${err}`);
      });
    } catch (error) {
      logger.error(error, `Failed to process hitl_approve: ${error}`);
    }
  });

  /**
   * Handle "Always Allow" button — approve and bypass this tool for future invocations.
   */
  app.action('hitl_approve_bypass_tool', async ({ ack, body, client }) => {
    await ack();
    const logger = Logger.getLogger('hitlButton');

    const userId = body.user?.id;
    const action = (body as any).actions?.[0];
    const actionValue = action?.value || '';
    const channelId = (body as any).channel?.id;
    const messageTs = (body as any).message?.ts;
    const threadTs = (body as any).message?.thread_ts || messageTs;

    if (!actionValue || !userId || !channelId || !messageTs) {
      logger.warn(`Missing required values in hitl_approve_bypass_tool action`);
      return;
    }

    try {
      const decodedValue = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { taskId, toolName } = decodedValue;

      logger.info(`HITL approve+bypass_tool by user ${userId} for task ${taskId} tool ${toolName}`);

      await recordDecision(client, channelId, messageTs, decodedValue.streamMessageTs, 'Approved', `${decodedValue.summary || decodedValue.toolName} — always allow`, true);

      const decisions = { decisions: [{ type: 'approve', bypass: true, bypass_all: true }] };
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
        planMessageTs: decodedValue.planMessageTs,
        resumeStreamTs: decodedValue.streamMessageTs,
      };

      handleIncomingMessage(syntheticMessage, makeDeps()).catch((err) => {
        logger.error(err, `Failed to send HITL approve+bypass_tool to orchestrator: ${err}`);
      });
    } catch (error) {
      logger.error(error, `Failed to process hitl_approve_bypass_tool: ${error}`);
    }
  });

  /**
   * Handle "Allow Pattern" button — approve and bypass this specific pattern for future invocations.
   */
  app.action('hitl_approve_bypass_pattern', async ({ ack, body, client }) => {
    await ack();
    const logger = Logger.getLogger('hitlButton');

    const userId = body.user?.id;
    const action = (body as any).actions?.[0];
    const actionValue = action?.value || '';
    const channelId = (body as any).channel?.id;
    const messageTs = (body as any).message?.ts;
    const threadTs = (body as any).message?.thread_ts || messageTs;

    if (!actionValue || !userId || !channelId || !messageTs) {
      logger.warn(`Missing required values in hitl_approve_bypass_pattern action`);
      return;
    }

    try {
      const decodedValue = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { taskId, toolName } = decodedValue;

      logger.info(`HITL approve+bypass_pattern by user ${userId} for task ${taskId} tool ${toolName}`);

      await recordDecision(client, channelId, messageTs, decodedValue.streamMessageTs, 'Approved', `${decodedValue.summary || decodedValue.toolName} — pattern allowed`, true);

      const decisions = { decisions: [{ type: 'approve', bypass: true, bypass_pattern: decodedValue.matchedPattern }] };
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
        planMessageTs: decodedValue.planMessageTs,
        resumeStreamTs: decodedValue.streamMessageTs,
      };

      handleIncomingMessage(syntheticMessage, makeDeps()).catch((err) => {
        logger.error(err, `Failed to send HITL approve+bypass_pattern to orchestrator: ${err}`);
      });
    } catch (error) {
      logger.error(error, `Failed to process hitl_approve_bypass_pattern: ${error}`);
    }
  });

  /**
   * Handle "Request Changes" button - open modal showing proposed content
   * with a text area for the user to describe what should be different.
   * Sends a reject with the user's feedback so the LLM re-proposes.
   */
  app.action('hitl_request_changes', async ({ ack, body, client }) => {
    await ack();
    const logger = Logger.getLogger('hitlButton');

    const userId = body.user?.id;
    const action = (body as any).actions?.[0];
    const actionValue = action?.value || '';
    const triggerId = (body as any).trigger_id;
    const messageTs = (body as any).message?.ts;

    if (!actionValue || !userId || !triggerId) {
      logger.warn(`Missing required values in hitl_request_changes action`);
      return;
    }

    try {
      const decodedValue = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { taskId, toolName, channelId, threadTs } = decodedValue;

      logger.info(`HITL request changes clicked by user ${userId} for task ${taskId} tool ${toolName}`);

      const privateMetadata = JSON.stringify({
        taskId,
        toolName,
        channelId,
        threadTs,
        messageTs,
        planMessageTs: decodedValue.planMessageTs,
        streamMessageTs: decodedValue.streamMessageTs,
        summary: decodedValue.summary,
      });

      const toolLabel = (toolName || 'unknown').replace(/_/g, ' ');

      // Modal only needs a feedback input — proposed content is already visible in chat
      const blocks: any[] = [
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text: `*Tool:* ${toolLabel}\nDescribe what should be different and the agent will revise its proposal.`,
          },
        },
        { type: 'divider' },
        {
          type: 'input',
          block_id: 'feedback_block',
          label: {
            type: 'plain_text',
            text: 'What should be changed?',
            emoji: true,
          },
          element: {
            type: 'plain_text_input',
            action_id: 'feedback_input',
            multiline: true,
            placeholder: {
              type: 'plain_text',
              text: 'e.g. "Make the description shorter" or "Change scope to group"',
            },
          },
        },
      ];

      await client.views.open({
        trigger_id: triggerId,
        view: {
          type: 'modal',
          callback_id: 'hitl_submit',
          private_metadata: privateMetadata,
          title: {
            type: 'plain_text',
            text: 'Request Changes',
            emoji: true,
          },
          submit: {
            type: 'plain_text',
            text: 'Submit Feedback',
            emoji: true,
          },
          close: {
            type: 'plain_text',
            text: 'Cancel',
            emoji: true,
          },
          blocks,
        },
      });
      // Leave the widget intact while the modal is open; it's replaced with a
      // decision summary on submit (and stays usable if the modal is cancelled).
    } catch (error) {
      logger.error(error, `Failed to open HITL request changes modal: ${error}`);
    }
  });

  /**
   * Handle "Review & decide" button (multi-action interrupts) — open a modal with
   * one Approve/Reject radio per pending call so the user decides each individually.
   * Submitted as a batch by the hitl_multi_submit view handler.
   */
  app.action('hitl_review_multi', async ({ ack, body, client }) => {
    await ack();
    const logger = Logger.getLogger('hitlButton');

    const userId = body.user?.id;
    const action = (body as any).actions?.[0];
    const actionValue = action?.value || '';
    const triggerId = (body as any).trigger_id;
    const messageTs = (body as any).message?.ts;

    if (!actionValue || !userId || !triggerId) {
      logger.warn(`Missing required values in hitl_review_multi action`);
      return;
    }

    try {
      const decoded = JSON.parse(Buffer.from(actionValue, 'base64').toString());
      const { taskId, contextId, channelId, threadTs, calls } = decoded;
      const callList: any[] = Array.isArray(calls) ? calls : [];

      const blocks: any[] = [];
      callList.forEach((c: any, idx: number) => {
        const label = String(c?.name || 'tool').replace(/_/g, ' ');
        if (idx > 0) blocks.push({ type: 'divider' });
        blocks.push({
          type: 'section',
          text: { type: 'mrkdwn', text: `*${idx + 1}. ${label}*${c?.detail ? `\n${String(c.detail)}` : ''}` },
        });
        const approveOption = { text: { type: 'plain_text', text: 'Approve' }, value: 'approve' };
        const options: any[] = [approveOption];
        // Bypass variants — only for risk-scored tools, mirroring the single-action card.
        if (c?.risk) {
          options.push({ text: { type: 'plain_text', text: 'Approve · always allow this tool' }, value: 'approve_bypass_tool' });
          if (c?.pattern) {
            options.push({ text: { type: 'plain_text', text: 'Approve · allow this pattern' }, value: 'approve_bypass_pattern' });
          }
        }
        options.push({ text: { type: 'plain_text', text: 'Reject' }, value: 'reject' });
        blocks.push({
          type: 'input',
          block_id: `call_${idx}`,
          label: { type: 'plain_text', text: 'Decision', emoji: true },
          element: {
            type: 'radio_buttons',
            action_id: `decision_${idx}`,
            initial_option: approveOption,
            options,
          },
        });
      });

      // Only routing data + per-call id/pattern in private_metadata (kept well under
      // Slack's 3000-char limit); display content already shown in-channel.
      const privateMetadata = JSON.stringify({
        taskId,
        contextId,
        channelId,
        threadTs,
        messageTs,
        planMessageTs: decoded.planMessageTs,
        streamMessageTs: decoded.streamMessageTs,
        // Keep name + detail so the post-decision summary can show what was
        // approved/rejected (kept compact — well under Slack's 3000-char limit).
        calls: callList.map((c: any) => ({
          id: c?.id,
          name: c?.name,
          detail: typeof c?.detail === 'string' ? c.detail.substring(0, 120) : undefined,
          pattern: c?.pattern,
        })),
      });

      await client.views.open({
        trigger_id: triggerId,
        view: {
          type: 'modal',
          callback_id: 'hitl_multi_submit',
          private_metadata: privateMetadata,
          title: { type: 'plain_text', text: 'Review actions', emoji: true },
          submit: { type: 'plain_text', text: 'Submit', emoji: true },
          close: { type: 'plain_text', text: 'Cancel', emoji: true },
          blocks,
        },
      });
      // Leave the widget intact while the modal is open; it's replaced with a
      // decision summary on submit (and stays usable if the modal is cancelled).
    } catch (error) {
      logger.error(error, `Failed to open multi-action HITL modal: ${error}`);
    }
  });

  logger.info('Registered HITL action handlers');
}
