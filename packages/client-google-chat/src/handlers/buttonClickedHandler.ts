import { randomUUID } from 'crypto';
import { FeedbackService } from '../services/feedbackService.js';
import { ContextRecord, IContextStore } from '../storage/types.js';
import { Logger } from '../utils/logger.js';
import { handleIncomingMessage, NormalizedMessage } from './messageHandler.js';
import { HandlerDependencies } from "./types.js";
import { GoogleChatService } from '../services/googleChatService.js';

export interface ButtonClickedPayload {
  cardId: string,
  action: string,
  actionParameters: Record<string, string>;
  formInputs?: Record<string, { stringInputs?: { value: string[] } }>;
  userId: string;
  userEmail: string;
  projectId: string;
  spaceId: string;
  messageId: string;
  threadId: string;
}

interface ButtonFeedbackCardClickedParameters {
  taskId: string;
  subAgents?: string[];
}

interface ButtonHitlCardClickedParameters {
  taskId: string;
  toolName?: string;
  matchedPattern?: string;
}

async function handleFeedbackCardClick(
  chatService: GoogleChatService,
  feedbackService: FeedbackService,
  contextStore: IContextStore,
  payload: ButtonClickedPayload,
) {
  const logger = Logger.getLogger('handleFeedbackCardClick');

  const contextKey = contextStore.buildKey(payload.projectId, payload.spaceId, payload.threadId);
  const existingContext: ContextRecord | null = await contextStore.get(contextKey);
  const contextId = existingContext?.contextId;

  const actionParameters = payload.actionParameters as unknown as ButtonFeedbackCardClickedParameters;
  const rating = payload.action === 'yes' ? 'positive' : 'negative';

  if (!contextId) {
    logger.warn(`No context found for key=${contextKey}, cannot submit feedback`);
    return;
  }

  const subAgentId = Array.isArray(actionParameters.subAgents) && actionParameters.subAgents.length > 0 ? actionParameters.subAgents[0] : undefined;

  try {
    await feedbackService.submitFeedback(
      payload.userId,
      payload.projectId,
      contextId,
      actionParameters.taskId,
      rating,
      actionParameters.taskId,
      subAgentId
    );

    await chatService.updateMessage({
      projectId: payload.projectId,
      messageName: payload.messageId,
      text: '✅ Thanks for the feedback!',
      cardsV2: [],
    });

    logger.info(`Submitted ${rating} feedback for context=${contextId} taskId=${actionParameters.taskId}`);

  } catch (err) {
    logger.error(err, `Failed to submit feedback: ${err}`);
  }
}


async function handleHitlCardClick(payload: ButtonClickedPayload, deps: HandlerDependencies) {
  const logger = Logger.getLogger('handleHitlCardClick');

  const actionParameters = payload.actionParameters as unknown as ButtonHitlCardClickedParameters;

  logger.info(`HITL action: ${payload.action} for taskId=${actionParameters.taskId}`);

  if (payload.action === 'request_changes') {
    // Replace the card with a feedback form
    const toolLabel = actionParameters.toolName || 'unknown';
    const feedbackCard = deps.chatService.buildHitlFeedbackCard(
      deps.config,
      toolLabel,
      { taskId: actionParameters.taskId },
    );

    await deps.chatService.updateMessage({
      projectId: payload.projectId,
      messageName: payload.messageId,
      cardsV2: [feedbackCard],
    });
    return;
  }

  // Determine confirmation text and decisions payload
  let confirmText: string;
  let decisions: Record<string, unknown>;

  if (payload.action === 'approve') {
    confirmText = '✅ Approved';
    decisions = { decisions: [{ type: 'approve' }] };
  } else if (payload.action === 'approve_bypass_tool') {
    confirmText = '✅ Approved (always allow this tool)';
    decisions = { decisions: [{ type: 'approve', bypass: true, bypass_all: true }] };
  } else if (payload.action === 'approve_bypass_pattern') {
    const matchedPattern = (actionParameters as any).matchedPattern;
    confirmText = `✅ Approved (pattern allowed: ${matchedPattern || 'unknown'})`;
    decisions = { decisions: [{ type: 'approve', bypass: true, bypass_pattern: matchedPattern }] };
  } else {
    confirmText = '❌ Rejected';
    // No message → the server supplies the default rejection text.
    decisions = { decisions: [{ type: 'reject' }] };
  }

  await deps.chatService.updateMessage({
    projectId: payload.projectId,
    messageName: payload.messageId,
    text: confirmText,
    cardsV2: [],
  });

  // Send as a synthetic message via handleIncomingMessage (no visible chat message)
  const syntheticMessage: NormalizedMessage = {
    userId: payload.userId,
    userEmail: payload.userEmail,
    projectId: payload.projectId,
    spaceId: payload.spaceId,
    messageId: `synthetic-${randomUUID()}`,
    threadId: payload.threadId,
    rawText: '',
    dataParts: [decisions],
    source: 'direct_message',
  };

  await handleIncomingMessage(syntheticMessage, deps);
}

/**
 * Handle the multi-action HITL card: "Approve all"/"Reject all" send a blanket
 * decision (server replicates), "Submit decisions" reads the per-call radios from
 * formInputs and sends one decision per call, echoing each call_id so the server
 * aligns decisions by id (no unseen-call approvals).
 */
async function handleHitlMultiCardClick(payload: ButtonClickedPayload, deps: HandlerDependencies) {
  const logger = Logger.getLogger('handleHitlMultiCardClick');
  const params = payload.actionParameters as unknown as { taskId?: string; calls?: Array<{ id?: string; pattern?: string }> };

  let confirmText: string;
  let decisions: Record<string, unknown>;

  if (payload.action === 'approve') {
    confirmText = '✅ Approved all';
    decisions = { decisions: [{ type: 'approve' }] };
  } else if (payload.action === 'reject') {
    confirmText = '❌ Rejected all';
    // No message → the server supplies the default rejection text.
    decisions = { decisions: [{ type: 'reject' }] };
  } else {
    // submit_multi — one decision per call, in action_request order, by id.
    const calls = Array.isArray(params.calls) ? params.calls : [];
    const decisionList = calls.map((c, idx) => {
      const selected = payload.formInputs?.[`decision_${idx}`]?.stringInputs?.value?.[0] || 'approve';
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
    decisions = { decisions: decisionList };
    const rejected = decisionList.filter((d) => d.type === 'reject').length;
    confirmText = `Submitted ${decisionList.length} decision(s) — ${decisionList.length - rejected} approved, ${rejected} rejected`;
    logger.info(`HITL multi-decision submitted for taskId=${params.taskId}: ${decisionList.length} decision(s)`);
  }

  await deps.chatService.updateMessage({
    projectId: payload.projectId,
    messageName: payload.messageId,
    text: confirmText,
    cardsV2: [],
  });

  const syntheticMessage: NormalizedMessage = {
    userId: payload.userId,
    userEmail: payload.userEmail,
    projectId: payload.projectId,
    spaceId: payload.spaceId,
    messageId: `synthetic-${randomUUID()}`,
    threadId: payload.threadId,
    rawText: '',
    dataParts: [decisions],
    source: 'direct_message',
  };

  await handleIncomingMessage(syntheticMessage, deps);
}

/**
 * Handle HITL feedback form submission (from the feedback card).
 */
async function handleHitlFeedbackCardClick(payload: ButtonClickedPayload, deps: HandlerDependencies) {
  const logger = Logger.getLogger('handleHitlFeedbackCardClick');

  const actionParameters = payload.actionParameters as unknown as ButtonHitlCardClickedParameters;

  if (payload.action === 'cancel') {
    // User cancelled — just remove the feedback card
    await deps.chatService.updateMessage({
      projectId: payload.projectId,
      messageName: payload.messageId,
      text: 'ℹ️ Feedback cancelled',
      cardsV2: [],
    });
    return;
  }

  // Extract feedback from form inputs
  const feedback = payload.formInputs?.feedback?.stringInputs?.value?.[0]?.trim();

  if (!feedback) {
    logger.warn(`No feedback provided in HITL feedback form for task ${actionParameters.taskId}`);
    await deps.chatService.updateMessage({
      projectId: payload.projectId,
      messageName: payload.messageId,
      text: 'ℹ️ No feedback provided — please try again',
      cardsV2: [],
    });
    return;
  }

  logger.info(`HITL feedback submitted for taskId=${actionParameters.taskId}: ${feedback.substring(0, 100)}`);

  await deps.chatService.updateMessage({
    projectId: payload.projectId,
    messageName: payload.messageId,
    text: `✏️ Changes requested: ${feedback.substring(0, 200)}`,
    cardsV2: [],
  });

  // Send reject decision with user's feedback so the LLM re-proposes
  const rejectMessage = `The user requested changes to this tool call. Please revise and try again.\n\nUser feedback: ${feedback}`;
  const decisions = { decisions: [{ type: 'reject', message: rejectMessage }] };

  const syntheticMessage: NormalizedMessage = {
    userId: payload.userId,
    userEmail: payload.userEmail,
    projectId: payload.projectId,
    spaceId: payload.spaceId,
    messageId: `synthetic-${randomUUID()}`,
    threadId: payload.threadId,
    rawText: '',
    dataParts: [decisions],
    source: 'direct_message',
  };

  await handleIncomingMessage(syntheticMessage, deps);
}

export async function handleButtonClicked(
  payload: ButtonClickedPayload,
  deps: HandlerDependencies
): Promise<void> {
  const { chatService, feedbackService, contextStore } = deps;

  const logger = Logger.getLogger('handleButtonClicked');
  logger.info(`Button clicked payload=${JSON.stringify(payload)} from user ${payload.userId} in space ${payload.spaceId}`);

  const cardId = payload.cardId;

  switch (cardId) {
    case 'feedback_card': {
      if (feedbackService) {
        await handleFeedbackCardClick(
          chatService,
          feedbackService,
          contextStore,
          payload,
        );
      }
      break;
    }

    case 'hitl_card': {
      await handleHitlCardClick(
        payload,
        deps,
      );

      break;
    }

    case 'hitl_multi_card': {
      await handleHitlMultiCardClick(
        payload,
        deps,
      );

      break;
    }

    case 'hitl_feedback_card': {
      await handleHitlFeedbackCardClick(
        payload,
        deps,
      );

      break;
    } 

    default:
      logger.warn(`Unknown card=${cardId}, ignoring`);
  }
}
