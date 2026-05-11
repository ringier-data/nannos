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

interface ButtonBugReportCardClickedParameters {
  taskId: string;
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


async function handleBugReportCardClick(payload: ButtonClickedPayload, deps: HandlerDependencies) {
  const logger = Logger.getLogger('handleBugReportCardClick');

  const actionParameters = payload.actionParameters as unknown as ButtonBugReportCardClickedParameters;

  logger.info(`Bug report action: ${payload.action} for taskId=${actionParameters.taskId}`);

  await deps.chatService.updateMessage({
    projectId: payload.projectId,
    messageName: payload.messageId,
    text: payload.action === 'approve' ? '✅ Confirmed' : '❌ Declined',
    cardsV2: [],
  });

  // Build decisions payload as structured data (DataPart)
  let decisions: Record<string, unknown>;
  if (payload.action === 'approve') {
    decisions = { decisions: [{ type: 'approve' }] };
  } else {
    decisions = { decisions: [{ type: 'reject', message: 'User declined' }] };
  }

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

    case 'bug_report_card': {
      await handleBugReportCardClick(
        payload,
        deps,
      );

      break;
    } 

    default:
      logger.warn(`Unknown card=${cardId}, ignoring`);
  }
}
