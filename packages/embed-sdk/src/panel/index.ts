// '@nannos/embed-sdk/panel' — the heavy chat-UI entry, deliberately separate
// from the root so hosts lazy-load it (`import('@nannos/embed-sdk/panel')`).

// The assembled panel + style isolation.
export { AssistantPanel, type AssistantPanelProps } from './assistant-panel';
export { ShadowPortal, type ShadowPortalProps } from './shadow-portal';
export { themeSheet, type NannosTheme } from '../styles/theme-sheet';

// The chat engine scope (per-surface socket/transport/conversations bundle).
export {
  NannosChatScope,
  useChatEngine,
  useChatEngineOptional,
  type NannosChatScopeProps,
  type ChatEngine,
  type ChatSettings,
  type PlaygroundMode,
} from './engine';

// Hooks.
export {
  useNannosChat,
  type UseNannosChatValue,
  type PendingApproval,
} from './hooks/use-nannos-chat';
export { useConversations, type UseConversationsValue } from './hooks/use-conversations';
export { useSocketEvent } from './hooks/use-socket-event';
export {
  useAttachments,
  type UseAttachmentsValue,
  type AttachmentItem,
} from './hooks/use-attachments';

// The panel building blocks (for hosts composing their own layout).
export { Thread, type ThreadProps } from './components/thread';
export { Composer, type ComposerProps } from './components/composer';
export { ConversationList, type ConversationListProps } from './components/conversation-list';
export { ContinueCard, type ContinueCardProps } from './components/continue-card';
export {
  ConversationHistoryProvider,
  ConversationHistoryOverlay,
  useConversationHistory,
  type ConversationHistoryProviderProps,
  type ConversationHistoryOverlayProps,
  type ConversationHistoryValue,
} from './components/conversation-history';
export { ConnectionStatus, type ConnectionStatusProps } from './components/connection-status';
export { ApprovalCard, type ApprovalCardProps } from './components/approval-card';
export { ApplyModeSwitch, type ApplyModeSwitchProps } from './components/apply-mode-switch';
export { WorkingBlock, type WorkingBlockProps } from './components/working-block';
export { ContextChip, type ContextChipProps } from './components/context-chip';
export {
  DevContextInspector,
  type DevContextInspectorProps,
} from './components/dev-context-inspector';
export {
  DevModeProvider,
  useDevMode,
  useDevModeControls,
  resolveDevMode,
  type DevModeValue,
} from './dev-mode';
export {
  ApplyModeProvider,
  useApplyMode,
  useApplyModeControls,
  readStoredApplyMode,
  type ApplyMode,
  type ApplyModeValue,
} from './apply-mode';
export {
  MessageFeedback,
  ConversationFeedbackProvider,
  useConversationFeedback,
  type MessageFeedbackProps,
  type ConversationFeedbackProviderProps,
  type ConversationFeedbackValue,
} from './components/message-feedback';
export { ReportIssueButton, type ReportIssueButtonProps } from './components/report-issue-dialog';
export { AudioRecorderButton, type AudioRecorderButtonProps } from './components/audio-recorder';

// Vendored AI Elements — recomposition imports keep a stable home here.
export * from '../components/ai-elements';
