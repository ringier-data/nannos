// Shim: chat utils (protocol helpers + presentational helpers) now live in the
// Embed SDK.
export {
  generateUUID,
  escapeHtml,
  truncateText,
  shortenIdentifier,
  capitalize,
  humanizeStatusText,
  getInitials,
  formatTimestamp,
  formatTime,
  isTaskComplete,
  shouldShowTaskProgress,
  getTaskState,
  getPartKind,
  getFileInfo,
  formatTaskStatusLabel,
  applyInlineMarkdown,
  convertMarkdownToHtml,
  extractPartTexts,
  shouldDisplayMessageParts,
  copyToClipboard,
} from '@nannos/embed-sdk';
