import { WebClient } from '@slack/web-api';
import { Logger } from './logger.js';
import _ from 'lodash';
import { Artifact, DataPart, FileWithBytes, FileWithUri, Task } from '@a2a-js/sdk';
import type { ThinkingStepsStreamer } from './thinkingStepsStreamer.js';

const logger = Logger.getLogger('taskResponseHandler');

/**
 * Get emoji for task state
 */
export function getStateEmoji(state?: Task['status']['state']): string {
  switch (state) {
    case 'completed':
      return '✅';
    case 'working':
    case 'submitted':
      return '⏳';
    case 'input-required':
      return '❓';
    case 'auth-required':
      return '🔐';
    case 'failed':
    case 'rejected':
      return '❌';
    case 'canceled':
      return '🚫';
    default:
      return '📋';
  }
}

/**
 * Get default message for task state
 */
export function getStateMessage(state?: Task['status']['state']): string {
  switch (state) {
    case 'completed':
      return 'Request completed!';
    case 'working':
      return 'Working on your request...';
    case 'submitted':
      return 'Request submitted...';
    case 'input-required':
      return 'I need more information to proceed.';
    case 'auth-required':
      return 'Authentication is required to proceed.';
    case 'failed':
      return 'Task failed. Please try again.';
    case 'rejected':
      return 'Request was rejected.';
    case 'canceled':
      return 'Task was canceled.';
    default:
      return 'Processing...';
  }
}

/**
 * Check if state is a terminated state (no more updates expected)
 * Terminal states: completed, failed, rejected, canceled
 */
export function isTerminatedState(state?: Task['status']['state']): boolean {
  return ['completed', 'failed', 'rejected', 'canceled'].includes(state || '');
}

/**
 * Check if state is an interrupted state (paused, awaiting user action)
 * Interrupted states: input-required, auth-required
 */
export function isInterruptedState(state?: Task['status']['state']): boolean {
  return ['input-required', 'auth-required'].includes(state || '');
}

/**
 * Check if state has a user-facing message that should be displayed immediately
 * This includes terminated states plus interrupted states waiting for user action
 */
export function isInterruptedOrTerminated(state?: Task['status']['state']): boolean {
  return ['completed', 'failed', 'rejected', 'canceled', 'input-required', 'auth-required'].includes(state || '');
}

/**
 * Context for Slack message operations
 */
export interface SlackMessageContext {
  channelId: string;
  threadTs: string;
  messageTs: string; // Original message for reactions
  statusMessageTs?: string; // Status message for updates
}

/**
 * Parameters for handling task response
 */
export interface HandleTaskResponseParams {
  task: Task;
  slackClient: WebClient;
  messageContext: SlackMessageContext;
}

/**
 * Result from handling task response
 */
export interface HandleTaskResponseResult {
  statusMessageTs?: string;
  handled: boolean;
}

/**
 * Check if an error is a Slack `msg_too_long` platform error.
 */
function isMsgTooLongError(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    _.get(err, 'code') === 'slack_webapi_platform_error' &&
    _.get(err, 'data.error') === 'msg_too_long'
  );
}

/**
 * Fallback: upload text as a snippet file when it exceeds Slack's message limit.
 */
async function uploadTextAsSnippet(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  text: string
): Promise<string | undefined> {
  try {
    await slackClient.filesUploadV2({
      channel_id: channelId,
      thread_ts: threadTs,
      content: text,
      filename: 'response.md',
      title: 'Response',
      initial_comment: '📄 The response was too long for a Slack message, so it has been uploaded as a file.',
    });
    logger.info('Uploaded long message as text snippet');
    return undefined;
  } catch (uploadErr) {
    logger.error(uploadErr, `Failed to upload text as snippet: ${uploadErr}`);
    return undefined;
  }
}

export async function postMessage(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  text: string
): Promise<string | undefined> {
  return postOrUpdateMessage(slackClient, channelId, threadTs, text, undefined);
}
/**
 * Post or update a status message.
 * Falls back to uploading a text snippet if the message exceeds Slack's size limit.
 */
export async function postOrUpdateMessage(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  text: string,
  existingTs?: string
): Promise<string | undefined> {
  try {
    if (existingTs) {
      await slackClient.chat.update({
        channel: channelId,
        ts: existingTs,
        markdown_text: text,
      });
      return existingTs;
    } else {
      const result = await slackClient.chat.postMessage({
        channel: channelId,
        thread_ts: threadTs,
        markdown_text: text,
      });
      return result.ts;
    }
  } catch (err) {
    if (isMsgTooLongError(err)) {
      logger.info('Message too long for Slack, uploading as text snippet');
      // If we had a status message, try to update it with a short note
      if (existingTs) {
        await slackClient.chat
          .update({
            channel: channelId,
            ts: existingTs,
            text: '📄 Response uploaded as a file (too long for a message).',
          })
          .catch(() => {});
      }
      return uploadTextAsSnippet(slackClient, channelId, threadTs, text);
    }
    logger.debug({ err, text }, `Failed to post/update message: ${err}`);
    return existingTs;
  }
}

/**
 * Extract text content and file artifacts from A2A artifacts
 */
export function processArtifacts(artifacts?: Artifact[]): {
  textParts: string[];
  filesWithBytes: FileWithBytes[];
  filesWithUri: FileWithUri[];
  dataParts: DataPart[];
} {
  const textParts: string[] = [];
  const filesWithBytes: FileWithBytes[] = [];
  const filesWithUri: FileWithUri[] = [];
  const dataParts: DataPart[] = [];

  if (artifacts) {
    for (const artifact of artifacts) {
      for (const part of artifact.parts) {
        if (part.kind === 'text') {
          textParts.push(part.text);
        } else if (part.kind === 'file') {
          if ('bytes' in part.file && part.file.bytes) {
            filesWithBytes.push(part.file);
          } else if ('uri' in part.file && part.file.uri) {
            filesWithUri.push(part.file);
          } else {
            logger.warn(`Unsupported kind in artifact ${artifact.artifactId}: ${part.kind}`);
          }
        } else if (part.kind === 'data') {
          dataParts.push(part);
        } else {
          logger.warn(`Unsupported part kind: ${_.get(part, 'kind')}`);
        }
      }
    }
  }
  return { textParts, filesWithBytes, filesWithUri, dataParts };
}

/**
 * Upload file artifacts to Slack
 */
export async function uploadFileArtifacts(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  files: Array<FileWithBytes>
): Promise<void> {
  if (files.length === 0) return;

  logger.info(`Uploading ${files.length} file artifact(s) to Slack`);

  for (const file of files) {
    try {
      await slackClient.filesUploadV2({
        channel_id: channelId,
        thread_ts: threadTs,
        file: Buffer.from(file.bytes, 'base64'),
        filename: file.name,
        initial_comment: file.name,
      });
      logger.debug(`Successfully uploaded file artifact: ${file.name}`);
    } catch (uploadError) {
      logger.error(uploadError, `Failed to upload file artifact ${file.name}: ${uploadError}`);
      // Notify user that file upload failed
      await slackClient.chat
        .postMessage({
          channel: channelId,
          thread_ts: threadTs,
          text: `⚠️ Failed to upload file: ${file.name}`,
        })
        .catch(() => {});
    }
  }
}

/**
 * Handle a complete task response - posts messages and uploads artifacts
 * This is the main entry point for A2A Task -> Slack
 */
export async function handleTask(params: HandleTaskResponseParams): Promise<{ messageTs: string | undefined }> {
  const { task, slackClient, messageContext } = params;

  const { channelId, threadTs, messageTs, statusMessageTs } = messageContext;

  // Check if we should display a message (final or input-required states)
  if (!isInterruptedOrTerminated(task.status.state)) {
    logger.info({ taskId: task.id }, `Task state is still processing, will not post new message: ${task.status.state}`);
    return { messageTs: undefined };
  }

  // Process artifacts for completed tasks
  const parts = processArtifacts(task.artifacts);

  let message = '';

  // For interrupted states (input-required, auth-required), the authoritative
  // message is in status.message — artifacts are just intermediate streaming
  // tokens from BEFORE the interrupt fired, not the final response.
  if (isInterruptedState(task.status.state) && task.status?.message?.parts) {
    for (const part of task.status.message.parts) {
      if (part.kind === 'text') {
        message += (part as { kind: 'text'; text: string }).text;
      }
    }
  }

  // For terminal states, use artifact text as the message
  if (!message && parts.textParts.length > 0) {
    message = parts.textParts.join('');
  }

  // Final fallback: extract from status message (e.g. completed with no artifacts)
  if (!message && task.status?.message?.parts) {
    for (const part of task.status.message.parts) {
      if (part.kind === 'text') {
        message += (part as { kind: 'text'; text: string }).text;
      }
    }
  }

  const urls = parts.filesWithUri.map((file) => file.uri);
  if (urls.length > 0) {
    message += `\n\nAttached files:\n${urls.join('\n')}`;
  }

  message = message.trim();
  // Update or post the message
  let postedMessageTs: string | undefined;
  if (message) {
    postedMessageTs = await postMessage(slackClient, channelId, threadTs, message);
  }

  // Upload file artifacts
  await uploadFileArtifacts(slackClient, channelId, threadTs, parts.filesWithBytes);

  return { messageTs: postedMessageTs || statusMessageTs || messageTs };
}

/**
 * Finalize a task whose answer was streamed via a {@link ThinkingStepsStreamer}.
 *
 * Mirrors {@link handleTask}'s text-extraction logic, but instead of posting a
 * fresh message it (a) streams the answer text if it wasn't already streamed
 * chunk-by-chunk from artifact-update events, (b) appends file-URI links as
 * trailing markdown, (c) stops the stream, and (d) uploads byte file artifacts.
 *
 * `handleTask` stays as-is for the non-streaming paths (startup recovery,
 * async webhook completion).
 */
export async function finalizeStreamedTask(params: {
  task: Task;
  streamer: ThinkingStepsStreamer;
  slackClient: WebClient;
  messageContext: SlackMessageContext;
}): Promise<{ messageTs: string | undefined }> {
  const { task, streamer, slackClient, messageContext } = params;
  const { channelId, threadTs, messageTs, statusMessageTs } = messageContext;

  if (!isInterruptedOrTerminated(task.status.state)) {
    logger.info({ taskId: task.id }, `Task state still processing, not finalizing stream: ${task.status.state}`);
    return { messageTs: undefined };
  }

  const parts = processArtifacts(task.artifacts);

  // Resolve the authoritative answer text (same precedence as handleTask):
  // interrupted → status.message; terminal → artifact text; fallback status.message.
  let message = '';
  if (isInterruptedState(task.status.state) && task.status?.message?.parts) {
    for (const part of task.status.message.parts) {
      if (part.kind === 'text') message += (part as { kind: 'text'; text: string }).text;
    }
  }
  if (!message && parts.textParts.length > 0) {
    message = parts.textParts.join('');
  }
  if (!message && task.status?.message?.parts) {
    for (const part of task.status.message.parts) {
      if (part.kind === 'text') message += (part as { kind: 'text'; text: string }).text;
    }
  }
  message = message.trim();

  // The terminal status/artifact text is the AUTHORITATIVE full answer (the
  // server's `final_answer_source: "fallback"` contract). Hand it to the
  // streamer as a snapshot: it appends only what the live artifact-append
  // stream didn't already show — nothing in the common case (deduping the
  // body), or the missing suffix if an intermediate SSE frame was dropped.
  if (message) {
    await streamer.appendAnswer(message, true);
  }

  // Footer: linked filenames rather than bare URLs.
  const fileLinks = parts.filesWithUri.map((f) => `• <${f.uri}|${f.name || 'file'}>`);
  const trailingMarkdown = fileLinks.length > 0 ? `\n\n*Attached files:*\n${fileLinks.join('\n')}` : undefined;

  // Settle the (collapsed) plan disclosure to a finished label on success —
  // otherwise it stays "Working" after completion.
  const planTitle = isTerminatedState(task.status.state) ? 'Thinking' : undefined;
  await streamer.finish({ trailingMarkdown, planTitle });

  // File (byte) artifacts upload as separate Slack files, as before.
  await uploadFileArtifacts(slackClient, channelId, threadTs, parts.filesWithBytes);

  // Prefer the final-answer message for feedback/reactions; fall back to the
  // thinking widget if there was no separate answer message.
  return { messageTs: streamer.answerTs || streamer.ts || statusMessageTs || messageTs };
}

/** rich_text block wrapping one line of plain text (for a task-card's details). */
export function decisionRichText(text: string, max = 2000): any {
  return {
    type: 'rich_text',
    elements: [{ type: 'rich_text_section', elements: [{ type: 'text', text: text.substring(0, max) }] }],
  };
}

/**
 * Replace a HITL approval widget (its own message) with a concise decision
 * summary after the user decides. Tries a compact collapsible `task_card`
 * (title = the decision, details = specifics); falls back to a plain section if
 * task cards aren't supported in a non-streamed message.
 */
export async function replaceInterruptWithDecision(
  slackClient: WebClient,
  channelId: string,
  ts: string,
  title: string,
  detail?: string
): Promise<void> {
  const fallbackText = detail ? `${title} — ${detail}` : title;
  try {
    await slackClient.chat.update({
      channel: channelId,
      ts,
      text: fallbackText,
      blocks: [
        {
          type: 'task_card',
          task_id: 'hitl_decision',
          title: title.substring(0, 256),
          status: 'complete',
          ...(detail ? { details: decisionRichText(detail) } : {}),
        },
      ],
    });
    return;
  } catch (err) {
    logger.debug({ err }, `task_card decision summary unsupported, using a section`);
  }
  await slackClient.chat
    .update({
      channel: channelId,
      ts,
      text: fallbackText,
      blocks: [{ type: 'section', text: { type: 'mrkdwn', text: `*${title}*${detail ? ` — ${detail}` : ''}` } }],
    })
    .catch((err) => logger.debug({ err }, `Failed to post decision summary`));
}


/**
 * Record a HITL decision. Preferred: append a decision task card to the OPEN
 * thinking-steps stream (so the outcome shows inside that one widget) and remove
 * the standalone approval message. If there's no open stream (or appending fails
 * — e.g. it expired), fall back to turning the approval message itself into the
 * decision summary.
 */
export async function recordDecision(
  slackClient: WebClient,
  channelId: string,
  approvalMessageTs: string,
  streamTs: string | undefined,
  title: string,
  detail: string | undefined,
  approved: boolean
): Promise<void> {
  if (streamTs) {
    try {
      await slackClient.chat.appendStream({
        channel: channelId,
        ts: streamTs,
        chunks: [
          {
            type: 'task_update',
            // Unique per interrupt so multiple HITL decisions don't overwrite each other.
            id: `hitl_decision:${approvalMessageTs}`,
            title: title.substring(0, 256),
            status: approved ? 'complete' : 'error',
            ...(detail ? { details: detail.substring(0, 2000) } : {}),
          },
        ],
      } as any);
      // Decision now lives in the thinking widget — drop the standalone approval message.
      // If the delete fails (rate-limited/expired), don't leave its live Approve/Reject
      // buttons clickable: fall through to replacing the message with a static summary.
      try {
        await slackClient.chat.delete({ channel: channelId, ts: approvalMessageTs });
        return;
      } catch (err) {
        logger.debug({ err }, `could not delete approval message ${approvalMessageTs}; replacing with summary`);
      }
    } catch (err) {
      logger.debug({ err }, `could not append decision to stream ${streamTs}; using standalone card`);
    }
  }
  await replaceInterruptWithDecision(slackClient, channelId, approvalMessageTs, title, detail);
}

/**
 * Handle error case - update reactions and post error message
 */
export async function handleError(
  slackClient: WebClient,
  channelId: string,
  threadTs: string,
  messageTs: string,
  errorMessage: string = 'An error occurred while processing your request. Please try again.'
): Promise<void> {
  // Remove eyes reaction
  try {
    await slackClient.reactions.remove({
      channel: channelId,
      name: 'eyes',
      timestamp: messageTs,
    });
  } catch (e) {
    // Ignore - reaction may not exist
  }

  await slackClient.chat
    .postMessage({
      channel: channelId,
      thread_ts: threadTs,
      text: `❌ ${errorMessage}`,
    })
    .catch((err) => logger.error(err, `Failed to send error message: ${err}`));
}



/**
 * Build a generic HITL interrupt widget with Approve/Decline buttons.
 * Works for any tool that triggers a human-in-the-loop interrupt.
 */
export interface HitlInterruptWidgetData {
  taskId: string;
  contextId: string;
  toolName: string;
  reason: string;
  channelId: string;
  threadTs: string;
  actionRequests?: any[];
  reviewConfigs?: Array<{ action_name: string; allowed_decisions: string[] }>;
  planMessageTs?: string; // Existing plan-widget ts, carried through the HITL resume
  streamMessageTs?: string; // Open thinking-steps stream ts, carried through the HITL resume
}

export function buildHitlInterruptWidget(data: HitlInterruptWidgetData): any[] {
  // Determine allowed decisions from review_configs
  const reviewConfig = data.reviewConfigs?.find(rc => rc.action_name === data.toolName);
  const allowedDecisions = reviewConfig?.allowed_decisions ?? ['approve', 'reject'];

  const toolLabel = data.toolName.replace(/_/g, ' ');

  // Extract proposed args for display
  const CONTENT_KEYS = ['content', 'body', 'description'];
  const HIDDEN_KEYS = ['reason', '_risk_metadata'];
  const firstAction = data.actionRequests?.[0];
  const toolArgs = firstAction?.args || {};
  const contentKey = CONTENT_KEYS.find((k) => k in toolArgs);
  const proposedContent = contentKey ? String(toolArgs[contentKey] || '') : '';
  const metaEntries = Object.entries(toolArgs).filter(
    ([k]) => !CONTENT_KEYS.includes(k) && !HIDDEN_KEYS.includes(k)
  );

  // Extract risk metadata for bypass buttons
  const riskMeta = toolArgs._risk_metadata as { source?: string; score?: number; threshold?: number; matched_pattern?: string | null } | undefined;
  const isRiskScored = riskMeta?.source === 'risk_score';

  // Concise "what was decided" summary for the post-decision card details.
  const argSummary = metaEntries
    .map(([, v]) => String(v))
    .join(' ')
    .substring(0, 200);
  const decisionSummary = `${toolLabel}${argSummary ? ` ${argSummary}` : ''}`;

  // Button payload includes routing info + matched pattern for bypass
  const payload = {
    taskId: data.taskId,
    contextId: data.contextId,
    toolName: data.toolName,
    channelId: data.channelId,
    threadTs: data.threadTs,
    allowedDecisions,
    summary: decisionSummary,
    ...(data.planMessageTs ? { planMessageTs: data.planMessageTs } : {}),
    ...(data.streamMessageTs ? { streamMessageTs: data.streamMessageTs } : {}),
    ...(isRiskScored && riskMeta?.matched_pattern ? { matchedPattern: riskMeta.matched_pattern } : {}),
  };
  const encodedData = Buffer.from(JSON.stringify(payload)).toString('base64');

  // Build action buttons: always Approve + Reject, optionally Request Changes and bypass buttons
  const editAllowed = allowedDecisions.includes('edit');
  const approveAllowed = allowedDecisions.includes('approve');
  const actionElements: any[] = [];

  if (approveAllowed) {
    actionElements.push({
      type: 'button',
      text: { type: 'plain_text', text: 'Approve' },
      action_id: 'hitl_approve',
      value: encodedData,
      style: 'primary',
    });
  }

  if (editAllowed) {
    actionElements.push({
      type: 'button',
      text: { type: 'plain_text', text: 'Request changes' },
      action_id: 'hitl_request_changes',
      value: encodedData,
    });
  }

  // Bypass buttons — only for risk-scored tools
  if (isRiskScored && approveAllowed) {
    if (riskMeta!.matched_pattern) {
      actionElements.push({
        type: 'button',
        text: { type: 'plain_text', text: 'Allow pattern' },
        action_id: 'hitl_approve_bypass_pattern',
        value: encodedData,
        // Bypass permanently widens auto-approval → confirm (Slack: require
        // explicit confirmation for high-impact/irreversible actions).
        confirm: confirmDialog(
          'Allow this pattern?',
          `Future calls matching \`${riskMeta!.matched_pattern}\` will run without asking. You can change this later.`,
          'Allow pattern'
        ),
      });
    }
    actionElements.push({
      type: 'button',
      text: { type: 'plain_text', text: 'Always allow' },
      action_id: 'hitl_approve_bypass_tool',
      value: encodedData,
      confirm: confirmDialog(
        'Always allow this tool?',
        `Future "${toolLabel}" calls will run without asking. You can change this later.`,
        'Always allow'
      ),
    });
  }

  actionElements.push({
    type: 'button',
    text: { type: 'plain_text', text: 'Reject' },
    action_id: 'hitl_reject',
    value: encodedData,
    style: 'danger',
  });

  // Header section + args laid out as two-column fields.
  const headerSection: any = {
    type: 'section',
    text: {
      type: 'mrkdwn',
      text: `*Approval required — ${toolLabel}*\n${data.reason.substring(0, 2000)}`,
    },
  };
  if (metaEntries.length > 0) {
    headerSection.fields = metaEntries
      .slice(0, 10)
      .map(([k, v]) => ({ type: 'mrkdwn', text: `*${k}:*\n${String(v).substring(0, 400)}` }));
  }
  const blocks: any[] = [headerSection];

  // Risk indicator (context line) for risk-scored tools.
  if (isRiskScored && riskMeta) {
    blocks.push({ type: 'context', elements: [{ type: 'mrkdwn', text: riskContextText(riskMeta) }] });
  }

  // Proposed content preview (truncated).
  if (proposedContent) {
    blocks.push({
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `*Proposed content:*\n\`\`\`${proposedContent.substring(0, 2500)}\`\`\``,
      },
    });
  }

  blocks.push({ type: 'actions', elements: actionElements });

  return blocks;
}

/** Stable per-call id the server uses to align decisions (top-level args._call_id). */
export function callIdOf(action: any): string | undefined {
  return action?.args?._call_id;
}

/**
 * Native "Are you sure?" confirmation dialog for high-impact buttons (bypass /
 * approve-all). Slack pops this before the action fires. Reserved for genuinely
 * consequential clicks to avoid confirmation fatigue.
 */
function confirmDialog(title: string, text: string, confirmLabel: string): any {
  return {
    title: { type: 'plain_text', text: title.substring(0, 100) },
    text: { type: 'plain_text', text: text.substring(0, 300) },
    confirm: { type: 'plain_text', text: confirmLabel.substring(0, 30) },
    deny: { type: 'plain_text', text: 'Cancel' },
    style: 'danger',
  };
}

/** Single source of truth for the risk-context line (label + score + pattern). */
function riskContextText(riskMeta: { score?: number; matched_pattern?: string | null }): string {
  const pct = Math.round((riskMeta.score ?? 0) * 100);
  const label = pct >= 90 ? 'Critical' : pct >= 80 ? 'High' : pct >= 60 ? 'Medium' : 'Low';
  let text = `Risk: *${label}* (${pct}%)`;
  if (riskMeta.matched_pattern) text += `  ·  matched \`${riskMeta.matched_pattern}\``;
  return text;
}

/**
 * Read-only detail blocks for ONE action_request: a section whose `fields` lay
 * the args out in two columns, a context line for risk, and a code block for any
 * proposed content. No per-action index prefix — dividers delimit actions.
 */
function buildActionDetailBlocks(action: any): any[] {
  const args = action?.args || {};
  const toolLabel = String(action?.name || 'unknown').replace(/_/g, ' ');
  const CONTENT_KEYS = ['content', 'body', 'description'];
  const HIDDEN_KEYS = ['reason', '_risk_metadata'];
  const contentKey = CONTENT_KEYS.find((k) => k in args);
  const proposedContent = contentKey ? String(args[contentKey] || '') : '';
  const metaEntries = Object.entries(args).filter(([k]) => !CONTENT_KEYS.includes(k) && !HIDDEN_KEYS.includes(k));
  const riskMeta = args._risk_metadata as { source?: string; score?: number; matched_pattern?: string | null } | undefined;
  const isRiskScored = riskMeta?.source === 'risk_score';
  const reason = String((args.description ?? args.reason) || '');

  const section: any = {
    type: 'section',
    text: { type: 'mrkdwn', text: `*${toolLabel}*${reason ? `\n${reason.substring(0, 1000)}` : ''}` },
  };
  // Args as compact two-column key/value fields (Slack caps at 10).
  if (metaEntries.length > 0) {
    section.fields = metaEntries
      .slice(0, 10)
      .map(([k, v]) => ({ type: 'mrkdwn', text: `*${k}:*\n${String(v).substring(0, 400)}` }));
  }
  const blocks: any[] = [section];
  if (isRiskScored && riskMeta) {
    blocks.push({ type: 'context', elements: [{ type: 'mrkdwn', text: riskContextText(riskMeta) }] });
  }
  if (proposedContent) {
    blocks.push({
      type: 'section',
      text: { type: 'mrkdwn', text: `*Proposed content:*\n\`\`\`${proposedContent.substring(0, 1500)}\`\`\`` },
    });
  }
  return blocks;
}

/**
 * Multi-action HITL widget for interrupts carrying more than one action_request
 * (e.g. parallel tool calls). Shows the full detail of EVERY call (so nothing is
 * approved unseen), with a blanket Approve all / Reject all and a "Review & decide"
 * button that opens a modal collecting one decision per call (batched submit).
 */
export function buildMultiHitlInterruptWidget(data: HitlInterruptWidgetData): any[] {
  const actions = data.actionRequests ?? [];
  const blocks: any[] = [
    {
      type: 'section',
      text: { type: 'mrkdwn', text: `*${actions.length} actions need your approval*` },
    },
  ];
  actions.forEach((action) => {
    blocks.push({ type: 'divider' });
    blocks.push(...buildActionDetailBlocks(action));
  });

  // Compact per-call routing for the modal — kept small so it fits in Slack's
  // button-value limit without a server-side store. `detail` summarizes the
  // distinguishing args (e.g. `path: /memories/`) so the modal rows are
  // distinguishable; risk/pattern drive the per-call bypass options.
  const CONTENT_KEYS = ['content', 'body', 'description'];
  const HIDDEN_KEYS = ['reason', '_risk_metadata'];
  const calls = actions.map((a) => {
    const args = a?.args || {};
    const riskMeta = args._risk_metadata as { source?: string; matched_pattern?: string | null } | undefined;
    const isRiskScored = riskMeta?.source === 'risk_score';
    const argSummary = Object.entries(args)
      .filter(([k]) => !CONTENT_KEYS.includes(k) && !HIDDEN_KEYS.includes(k))
      .map(([k, v]) => `${k}: ${String(v)}`)
      .join(', ');
    const detail = (argSummary || String((args.description ?? args.reason) || '')).substring(0, 180);
    return {
      id: callIdOf(a),
      name: a?.name || 'unknown',
      detail,
      risk: isRiskScored,
      pattern: isRiskScored ? (riskMeta?.matched_pattern || undefined) : undefined,
    };
  });
  // One-line-per-action summary for the post-decision card details.
  const decisionSummary = calls
    .map((c) => `${c.name}${c.detail ? ` — ${c.detail}` : ''}`)
    .join('\n')
    .substring(0, 500);

  const base = {
    taskId: data.taskId,
    contextId: data.contextId,
    channelId: data.channelId,
    threadTs: data.threadTs,
    summary: decisionSummary,
    ...(data.planMessageTs ? { planMessageTs: data.planMessageTs } : {}),
    ...(data.streamMessageTs ? { streamMessageTs: data.streamMessageTs } : {}),
  };
  const blanketValue = Buffer.from(JSON.stringify(base)).toString('base64');
  const reviewValue = Buffer.from(JSON.stringify({ ...base, calls })).toString('base64');

  blocks.push({ type: 'divider' });
  blocks.push({
    type: 'actions',
    elements: [
      {
        type: 'button',
        text: { type: 'plain_text', text: 'Approve all' },
        action_id: 'hitl_approve',
        value: blanketValue,
        style: 'primary',
        // Approving multiple tools in one click is high-impact → confirm.
        confirm: confirmDialog(
          `Approve all ${actions.length} actions?`,
          `All ${actions.length} pending tool calls will run. Use "Review & decide" to approve them individually.`,
          'Approve all'
        ),
      },
      { type: 'button', text: { type: 'plain_text', text: 'Reject all' }, action_id: 'hitl_reject', value: blanketValue, style: 'danger' },
      { type: 'button', text: { type: 'plain_text', text: 'Review & decide' }, action_id: 'hitl_review_multi', value: reviewValue },
    ],
  });
  return blocks;
}
