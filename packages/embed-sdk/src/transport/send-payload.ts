/**
 * Serialization of outgoing sends: the ONLY place a `send_message` payload is
 * assembled. Two shapes ride the same socket event:
 *  - a NEW TURN (typed text / injected prompt, optional attachments), and
 *  - a HITL RESUME (empty message + `dataParts: [{decisions}]`), which must
 *    re-attach `executeOnlySubAgentId` AND the client-object manifest because
 *    the orchestrator re-runs from the top on resume (ADR-0004; old
 *    ChatContext.tsx:1800-1812).
 */
import type { ManifestEntry } from '../core/types';
import type { NannosPageContext } from '../core/page-context';
import type { SendMessagePayload } from '../core/wire';
import { decodeApproval, type Decision } from './approval-codec';
import type { NannosUIMessage } from './ai-types';

/** Everything send-time that isn't the message itself; assembled by the host provider. */
export interface SendContext {
  sessionId: string;
  model?: string;
  enableThinking?: boolean;
  thinkingLevel?: string;
  clientObjects?: ManifestEntry[];
  /** The page the user is on RIGHT NOW (read at send time, so a steer sent
   *  after navigating carries the new location). */
  pageContext?: NannosPageContext;
  executeOnlySubAgentId?: string | number;
  subAgentConfigHash?: string;
  playgroundSubagentName?: string;
}

export type SendClassification =
  | { kind: 'new-turn'; userMessage: NannosUIMessage }
  | { kind: 'hitl-resume'; decisions: Decision[] }
  | { kind: 'empty' };

/**
 * Classify what `useChat` handed the transport: a user message ends the list
 * on a new turn; an assistant message whose dynamic-tool parts carry approval
 * responses is the auto-send that resumes a HITL interrupt.
 */
export function classifySend(messages: NannosUIMessage[]): SendClassification {
  const last = messages[messages.length - 1];
  if (!last) return { kind: 'empty' };
  if (last.role === 'user') return { kind: 'new-turn', userMessage: last };
  if (last.role === 'assistant') {
    const decisions: Decision[] = [];
    for (const part of last.parts) {
      if (part.type !== 'dynamic-tool') continue;
      if (part.state !== 'approval-responded') continue;
      const approval = part.approval as { id: string; approved: boolean; reason?: string };
      decisions.push(
        decodeApproval(approval.id, { approved: approval.approved, reason: approval.reason }),
      );
    }
    if (decisions.length > 0) return { kind: 'hitl-resume', decisions };
  }
  return { kind: 'empty' };
}

function baseMetadata(ctx: SendContext): Record<string, unknown> {
  const metadata: Record<string, unknown> = {};
  if (ctx.subAgentConfigHash) metadata.subAgentConfigHash = ctx.subAgentConfigHash;
  if (ctx.playgroundSubagentName) metadata.playgroundSubagentName = ctx.playgroundSubagentName;
  // On new turns AND HITL resumes (the orchestrator re-runs from the top on
  // resume, exactly like clientObjects).
  if (ctx.pageContext) metadata.pageContext = ctx.pageContext;
  if (ctx.executeOnlySubAgentId != null) {
    metadata.executeOnlySubAgentId = ctx.executeOnlySubAgentId;
    if (ctx.clientObjects?.length) metadata.clientObjects = ctx.clientObjects;
  } else if (ctx.clientObjects?.length) {
    metadata.clientObjects = ctx.clientObjects;
  }
  return metadata;
}

/** Plain text of a user message = its text parts joined. */
export function textOfUserMessage(message: NannosUIMessage): string {
  return message.parts
    .filter((p): p is Extract<typeof p, { type: 'text' }> => p.type === 'text')
    .map((p) => p.text)
    .join('\n');
}

export function buildNewTurnPayload(args: {
  messageId: string;
  conversationId: string;
  userMessage: NannosUIMessage;
  ctx: SendContext;
}): SendMessagePayload {
  const { messageId, conversationId, userMessage, ctx } = args;
  const metadata = baseMetadata(ctx);
  if (ctx.model) metadata.model = ctx.model;
  // Wire quirk kept from the old client: enableThinking crosses as a string.
  if (ctx.enableThinking !== undefined) metadata.enableThinking = String(ctx.enableThinking);
  if (ctx.thinkingLevel) metadata.thinkingLevel = ctx.thinkingLevel;
  const displayLabel = userMessage.metadata?.display?.label;
  if (displayLabel) metadata.injectedDisplayText = displayLabel;

  const attachments = userMessage.metadata?.attachments;
  return {
    id: messageId,
    conversationId,
    message: textOfUserMessage(userMessage),
    sessionId: ctx.sessionId,
    metadata,
    contextId: conversationId,
    ...(attachments?.length && { fileAttachments: attachments }),
  };
}

export function buildHitlResumePayload(args: {
  messageId: string;
  conversationId: string;
  decisions: Decision[];
  ctx: SendContext;
}): SendMessagePayload {
  const { messageId, conversationId, decisions, ctx } = args;
  return {
    id: messageId,
    conversationId,
    message: '',
    sessionId: ctx.sessionId,
    metadata: baseMetadata(ctx),
    contextId: conversationId,
    dataParts: [{ decisions }],
  };
}
