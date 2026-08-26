/**
 * The chat chrome's translatable strings — flat keys, `{placeholder}`
 * interpolation (see `format` in react/i18n.tsx; no ICU).
 *
 * The SDK ships English defaults (`./en`); a host passes a partial override
 * map via `<NannosProvider strings={…}>`. `nannosStringKeys` exists so a host
 * can build that map mechanically from its own i18n system:
 *
 *   Object.fromEntries(nannosStringKeys.map((k) => [k, t(`nannos.sdk.${k}`)]))
 */
export interface NannosStrings {
  // Panel chrome
  'panel.title': string;
  'panel.close': string;
  'panel.pin': string;
  'panel.unpin': string;
  'panel.newChat': string;
  'panel.history': string;
  'panel.settings': string;

  // Composer
  'composer.placeholder': string;
  'composer.placeholderStreaming': string;
  'composer.send': string;
  'composer.stop': string;
  'composer.attach': string;
  'composer.record': string;
  'composer.recordStop': string;
  'composer.recordPermission': string;
  'composer.recordError': string;
  'composer.readOnly': string;

  // Connection status
  'status.connected': string;
  'status.connecting': string;
  'status.disconnected': string;
  'status.unauthenticated': string;
  'status.signIn': string;
  'status.error': string;

  // Thread / conversation list
  'thread.emptyTitle': string;
  'thread.emptyHint': string;
  'thread.continueTitle': string;
  'thread.loadOlder': string;
  'thread.error': string;
  'thread.newConversation': string;
  'conversations.title': string;
  'conversations.search': string;
  'conversations.empty': string;
  'conversations.noMatches': string;
  'conversations.rename': string;
  'conversations.renameLabel': string;
  'conversations.renameError': string;
  'conversations.delete': string;
  'conversations.deleteTitle': string;
  'conversations.deleteBody': string;
  'conversations.deleteConfirm': string;
  'conversations.deleteCancel': string;
  'conversations.deleteError': string;

  // Injected-prompt context chip
  'context.label': string;

  // HITL approval card
  /** Card heading; `{toolName}` is the tool title(s) awaiting approval. */
  'hitl.title': string;
  'hitl.approve': string;
  'hitl.approveAll': string;
  'hitl.rejectAll': string;
  'hitl.reject': string;
  'hitl.requestChanges': string;
  'hitl.reasonPlaceholder': string;
  'hitl.risk': string;
  'hitl.riskCritical': string;
  'hitl.riskHigh': string;
  'hitl.riskMedium': string;
  'hitl.riskLow': string;
  /** Column headers of the `apply` approval's field diff (field → current → new). */
  'hitl.diff.field': string;
  'hitl.diff.current': string;
  'hitl.diff.new': string;
  /** Self-evident `client_action` kinds, described by the SDK instead of the
   *  agent — a closed enum needs no LLM to explain it, and this way the
   *  sentence is in the user's language. */
  'hitl.clientAction.apply': string;
  'hitl.clientAction.highlight': string;
  'hitl.clientAction.navigate': string;
  'hitl.clientAction.readCurrentPage': string;

  // Apply mode — how much the assistant may do to a form on its own
  'applyMode.heading': string;
  'applyMode.manual': string;
  'applyMode.allowEdits': string;
  'applyMode.manualHint': string;
  'applyMode.allowEditsHint': string;
  'applyMode.label': string;

  // Secondary-authorization prompt (a tool needs the user's consent)
  'auth.title': string;
  'auth.body': string;
  'auth.bodyTool': string;
  'auth.action': string;
  'auth.retryHint': string;
  'auth.doneAction': string;
  'auth.retryAction': string;
  'auth.doneChip': string;

  // Working / timeline blocks
  'working.title': string;
  'thinking.title': string;

  // Feedback + issue reporting
  'feedback.helpful': string;
  'feedback.notHelpful': string;
  'feedback.prompt': string;
  'report.title': string;
  'report.description': string;
  'report.placeholder': string;
  'report.submit': string;
  'report.cancel': string;
  'report.success': string;
  'report.error': string;

  // Attachments
  'attachments.uploading': string;
  'attachments.failed': string;
  'attachments.remove': string;
}

/** Every key, for hosts building their override map mechanically. */
export const nannosStringKeys = [
  'panel.title',
  'panel.close',
  'panel.pin',
  'panel.unpin',
  'panel.newChat',
  'panel.history',
  'panel.settings',
  'composer.placeholder',
  'composer.placeholderStreaming',
  'composer.send',
  'composer.stop',
  'composer.attach',
  'composer.record',
  'composer.recordStop',
  'composer.recordPermission',
  'composer.recordError',
  'composer.readOnly',
  'status.connected',
  'status.connecting',
  'status.disconnected',
  'status.unauthenticated',
  'status.signIn',
  'status.error',
  'thread.emptyTitle',
  'thread.emptyHint',
  'thread.continueTitle',
  'thread.loadOlder',
  'thread.error',
  'thread.newConversation',
  'conversations.title',
  'conversations.search',
  'conversations.empty',
  'conversations.noMatches',
  'conversations.rename',
  'conversations.renameLabel',
  'conversations.renameError',
  'conversations.delete',
  'conversations.deleteTitle',
  'conversations.deleteBody',
  'conversations.deleteConfirm',
  'conversations.deleteCancel',
  'conversations.deleteError',
  'context.label',
  'hitl.title',
  'hitl.approve',
  'hitl.reject',
  'hitl.requestChanges',
  'hitl.reasonPlaceholder',
  'hitl.risk',
  'hitl.riskCritical',
  'hitl.riskHigh',
  'hitl.riskMedium',
  'hitl.riskLow',
  'hitl.diff.field',
  'hitl.diff.current',
  'hitl.diff.new',
  'auth.title',
  'auth.body',
  'auth.bodyTool',
  'auth.action',
  'auth.retryHint',
  'auth.doneAction',
  'auth.retryAction',
  'auth.doneChip',
  'working.title',
  'thinking.title',
  'feedback.helpful',
  'feedback.notHelpful',
  'feedback.prompt',
  'report.title',
  'report.description',
  'report.placeholder',
  'report.submit',
  'report.cancel',
  'report.success',
  'report.error',
  'attachments.uploading',
  'attachments.failed',
  'attachments.remove',
] as const satisfies readonly (keyof NannosStrings)[];
