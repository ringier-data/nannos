/**
 * `@nannos/embed-sdk/transport` — the framework-free chat engine: the
 * `ChatTransport` bridging A2A-over-socket.io to the AI SDK's typed
 * UIMessage streams, plus the message model and codecs. No React in this
 * subtree; all `ai` symbols flow through `ai-types.ts`.
 */
export * from './ai-types';
export { A2AChatTransport, type A2ATransportDeps, type TurnLifecycleEvent } from './a2a-transport';
export { TurnSession, type TurnFinishReason } from './turn-session';
export { demux, createDemuxState, type DemuxState, type DemuxResult, type DemuxDone } from './demux';
export {
  classifySend,
  buildNewTurnPayload,
  buildHitlResumePayload,
  textOfUserMessage,
  type SendContext,
  type SendClassification,
} from './send-payload';
export { encodeApproval, decodeApproval, type Decision, type ApprovalResponse } from './approval-codec';
export { sliceFromCodePoint, codePointLength } from './offsets';
export { ConnectionStore, type ConnectionSnapshot } from './connection-store';
export { WireLog, labelAgentEvent, type WireLogEntry } from './wire-log';
export {
  ConversationsStore,
  MAX_CONVERSATION_TITLE,
  type ConversationMeta,
  type ConversationOrigin,
  type ConversationsSnapshot,
  type ConversationsStoreOptions,
} from './conversations-store';
export {
  rowsToUIMessages,
  findPendingInterrupt,
  appendRestoredInterrupt,
  type RestMessageRow,
  type RestoredInterrupt,
} from './history-mapper';
