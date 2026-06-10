import { google, chat_v1 } from 'googleapis';
import { Readable } from 'stream';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { FileWithBytes } from '@a2a-js/sdk';

const logger = Logger.getLogger('GoogleChatService');

/**
 * Options for sending a message to Google Chat
 */
export interface SendMessageOptions {
  projectId: string;
  spaceId: string;
  text?: string;
  cardsV2?: chat_v1.Schema$CardWithId[];
  threadId?: string; // Thread key to reply in
  messageReplyOption?: 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD' | 'REPLY_MESSAGE_OR_FAIL';
  /** When set, the message is only visible to this user (ephemeral). Must be a User resource name, e.g. "users/123456789" */
  privateMessageViewerName?: string;
}

/**
 * Options for updating a message in Google Chat
 */
export interface UpdateMessageOptions {
  projectId: string;
  messageName: string; // e.g. spaces/xxx/messages/yyy
  text?: string;
  cardsV2?: chat_v1.Schema$CardWithId[];
  updateMask?: string; // e.g. 'text' or 'cardsV2'
}

/**
 * Service for interacting with Google Chat API using service account credentials.
 * Google Chat bots use service accounts to send messages (not user OAuth tokens).
 */
export class GoogleChatService {
  private chatApis: { [projectId: string]: chat_v1.Chat } = {};
  /** Raw service-account credentials keyed by project number, used to create impersonating auth clients. */
  private credentials: { [projectId: string]: any } = {};

  constructor(config: Config) {
    for (const googleChatConfig of config.googleChatConfigs) {
      const auth = new google.auth.GoogleAuth({
        scopes: [
          'https://www.googleapis.com/auth/chat.bot',
          'https://www.googleapis.com/auth/chat.app.messages.readonly',
        ],
        credentials: googleChatConfig.googleApplicationCredentials,
      });

      this.credentials[googleChatConfig.projectNumber] = googleChatConfig.googleApplicationCredentials;
      this.chatApis[googleChatConfig.projectNumber] = google.chat({ version: 'v1', auth });
      logger.info(
        `GoogleChatService initialized for project ${googleChatConfig.projectName} (${googleChatConfig.projectNumber})`
      );
    }
  }

  /**
   * Create a Chat API client that impersonates the given user via domain-wide delegation.
   */
  private createUserImpersonatedClient(projectId: string, userEmail: string): chat_v1.Chat {
    const credentials = this.credentials[projectId];
    const auth = new google.auth.JWT({
      email: credentials.client_email,
      key: credentials.private_key,
      scopes: ['https://www.googleapis.com/auth/chat.messages.readonly'],
      subject: userEmail,
    });
    return google.chat({ version: 'v1', auth });
  }

  /**
   * Send a text message to a Google Chat space
   */
  async sendTextMessage(
    projectId: string,
    spaceId: string,
    text: string,
    threadId?: string
  ): Promise<chat_v1.Schema$Message> {
    return this.sendMessage({
      projectId,
      spaceId,
      text,
      threadId,
      messageReplyOption: threadId ? 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD' : undefined,
    });
  }

  /**
   * Send a text message privately (ephemeral) to a specific user in a space.
   * The message is only visible to that user.
   */
  async sendPrivateTextMessage(
    projectId: string,
    spaceId: string,
    userId: string,
    text: string,
    threadId?: string
  ): Promise<chat_v1.Schema$Message> {
    return this.sendMessage({
      projectId,
      spaceId,
      text,
      threadId,
      messageReplyOption: threadId ? 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD' : undefined,
      privateMessageViewerName: userId,
    });
  }

  /**
   * Send a card message privately (ephemeral) to a specific user in a space.
   * The message is only visible to that user.
   */
  async sendPrivateCardMessage(
    projectId: string,
    spaceId: string,
    userId: string,
    cardsV2: chat_v1.Schema$CardWithId[],
    threadId?: string
  ): Promise<chat_v1.Schema$Message> {
    return this.sendMessage({
      projectId,
      spaceId,
      cardsV2,
      threadId,
      messageReplyOption: threadId ? 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD' : undefined,
      privateMessageViewerName: userId,
    });
  }

  /**
   * Send a message (text, card, or both) to a Google Chat space
   */
  async sendMessage(options: SendMessageOptions): Promise<chat_v1.Schema$Message> {
    try {
      const requestBody: chat_v1.Schema$Message = {};

      if (options.text) {
        requestBody.text = options.text;
      }

      if (options.cardsV2) {
        requestBody.cardsV2 = options.cardsV2;
      }

      if (options.threadId) {
        requestBody.thread = {
          name: options.threadId,
        };
      }

      if (options.privateMessageViewerName) {
        requestBody.privateMessageViewer = { name: options.privateMessageViewerName };
      }

      const response = await this.chatApis[options.projectId].spaces.messages.create({
        parent: options.spaceId,
        requestBody,
        messageReplyOption: options.messageReplyOption,
      });

      logger.debug(`Message sent to ${options.spaceId}: ${response.data.name} (thread: ${response.data.thread?.name})`);
      return response.data;
    } catch (error) {
      logger.error(`Failed to send message to ${options.spaceId}: ${error}`);
      throw new Error(`Failed to send Google Chat message: ${error}`);
    }
  }

  /**
   * Update an existing message
   */
  async updateMessage(options: UpdateMessageOptions): Promise<chat_v1.Schema$Message> {
    try {
      const requestBody: chat_v1.Schema$Message = {};
      const updateMaskFields: string[] = [];

      if (options.text !== undefined) {
        requestBody.text = options.text;
        updateMaskFields.push('text');
      }

      if (options.cardsV2 !== undefined) {
        requestBody.cardsV2 = options.cardsV2;
        updateMaskFields.push('cardsV2');
      }

      const updateMask = options.updateMask || updateMaskFields.join(',');

      const response = await this.chatApis[options.projectId].spaces.messages.update({
        name: options.messageName,
        requestBody,
        updateMask,
      });

      logger.debug(`Message updated: ${options.messageName}`);
      return response.data;
    } catch (error) {
      logger.error(`Failed to update message ${options.messageName}: ${error}`);
      throw new Error(`Failed to update Google Chat message: ${error}`);
    }
  }

  /**
   * Download an attachment from Google Chat.
   *
   * First tries using the bot's own credentials (`chat.bot` scope). If that
   * fails with an access error and a `userEmail` is provided, retries via
   * domain-wide delegation – impersonating the user with
   * `chat.messages.readonly` so the service account can read any message the
   * user has access to.
   *
   * @param projectId - GCP project number
   * @param attachmentResourceName - The resource name of the attachment (e.g. spaces/xxx/messages/yyy/attachments/zzz)
   * @returns The attachment data as a Buffer, or null if download failed
   */
  async downloadAttachment(
    projectId: string,
    userEmail: string,
    attachmentMetadata: { resourceName: string; contentType: string; fileName: string }
  ): Promise<{
    data: Buffer;
    contentType: string;
    fileName: string;
  } | null> {
    // Try bot-level download first
    const botResult = await this.downloadAttachmentWithClient(this.chatApis[projectId], attachmentMetadata);
    if (botResult) return botResult;

    // Fallback: impersonate a user via domain-wide delegation.
    logger.info(
      `Bot cannot access attachment ${attachmentMetadata.resourceName}, retrying media.download with impersonation`
    );
    try {
      const userClient = this.createUserImpersonatedClient(projectId, userEmail);
      const botResult = await this.downloadAttachmentWithClient(userClient, attachmentMetadata);
      return botResult;
    } catch (error) {
      logger.warn(`User-impersonation media.download failed for ${attachmentMetadata.resourceName}: ${error}`);
    }

    return null;
  }

  /**
   * Internal helper – download an attachment using the provided Chat API client.
   */
  private async downloadAttachmentWithClient(
    client: chat_v1.Chat,
    attachmentMetadata: { resourceName: string; contentType: string; fileName: string }
  ): Promise<{
    data: Buffer;
    contentType: string;
    fileName: string;
  } | null> {
    try {
      const response = await client.media.download(
        { resourceName: attachmentMetadata.resourceName, alt: 'media' },
        { responseType: 'arraybuffer' }
      );

      return {
        data: Buffer.from(response.data as ArrayBuffer),
        contentType: attachmentMetadata.contentType || 'application/octet-stream',
        fileName: attachmentMetadata.fileName || 'attachment',
      };
    } catch (error: any) {
      if (error.status === 403) {
        logger.debug(`Cannot access attachment ${attachmentMetadata.resourceName}`);
      } else {
        logger.error(`Failed to download attachment ${attachmentMetadata.resourceName}`);
      }
      return null;
    }
  }

  /**
   * Upload files to a Google Chat space and send them as a single message with attachments.
   * Falls back to a text notification listing the filenames if any upload fails.
   *
   * @param projectId - GCP project number
   * @param spaceId - Space resource name (e.g. "spaces/AAAA")
   * @param threadId - Thread resource name to reply in
   * @param files - Files to upload
   * @param text - Optional accompanying text for the message
   */
  async uploadAndSendFileAttachments(
    projectId: string,
    spaceId: string,
    threadId: string,
    files: FileWithBytes[],
    text?: string
  ): Promise<chat_v1.Schema$Message | null> {
    if (files.length === 0) return null;

    const attachments: chat_v1.Schema$Attachment[] = [];

    for (const file of files) {
      try {
        const response = await this.chatApis[projectId].media.upload({
          parent: spaceId,
          requestBody: { filename: file.name },
          media: {
            mimeType: file.mimeType,
            body: Readable.from(file.bytes),
          },
        });
        if (response.data) {
          attachments.push(response.data as chat_v1.Schema$Attachment);
          logger.info(`Uploaded file attachment: ${file.name} (${file.mimeType})`);
        }
      } catch (error) {
        logger.error(`Failed to upload file ${file.name}: ${error}`);
        // Fall back to text notification for the whole batch
        const fileList = files.map((f) => `- ${f.name} (${f.mimeType})`).join('\n');
        const fallbackText = `⚠️ ${files.length} file(s) generated (upload failed):\n${fileList}`;
        return this.sendTextMessage(projectId, spaceId, fallbackText, threadId);
      }
    }

    // Send one message with all attachments
    const requestBody: chat_v1.Schema$Message = {
      attachment: attachments,
      thread: { name: threadId },
    };
    if (text) {
      requestBody.formattedText = text;
    }

    const result = await this.chatApis[projectId].spaces.messages.create({
      parent: spaceId,
      requestBody,
      messageReplyOption: 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD',
    });

    logger.info(`Sent message with ${attachments.length} attachment(s) to ${spaceId}`);
    return result.data;
  }

  /**
   * Build an authorize button card for prompting users to log in
   */
  buildAuthCard(authorizeUrl: string, title: string, message: string, buttonText: string): chat_v1.Schema$CardWithId {
    return {
      cardId: 'auth-prompt',
      card: {
        header: {
          title: title,
          subtitle: message,
          imageUrl: 'https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/lock/default/48px.svg',
          imageType: 'CIRCLE',
        },
        sections: [
          {
            widgets: [
              {
                buttonList: {
                  buttons: [
                    {
                      text: buttonText,
                      onClick: {
                        openLink: {
                          url: authorizeUrl,
                        },
                      },
                      color: {
                        red: 0.0,
                        green: 0.54,
                        blue: 0.86,
                        alpha: 1,
                      },
                    },
                  ],
                },
              },
            ],
          },
        ],
      },
    };
  }

  /**
   * Build a feedback card with "Yes" and "No" buttons for user feedback on a response.
   * The buttons will trigger the provided handler URL with the specified parameters.
   */
  buildFeedbackCard(
    config: Config,
    taskId: string,
    subAgents?: string[]
  ): chat_v1.Schema$CardWithId {
    const widgets: any[] = [];

    const parameters = { taskId, subAgents };

    // Show involved agents if provided
    if (subAgents && subAgents.length > 0) {
      widgets.push({
        textParagraph: {
          text: `<i>Agents involved: ${subAgents.join(', ')}</i>`,
        },
      });
    }

    // Feedback buttons
    const buttonClickHandlerUrl = new URL(`/api/v1/chat/events`, config.baseUrl).toString();
    widgets.push({
      buttonList: {
        buttons: [
          {
            text: '👍 Yes',
            onClick: {
              action: {
                function: buttonClickHandlerUrl,
                parameters: [
                  { key: 'cardId', value: 'feedback_card' },
                  { key: 'action', value: 'yes' },
                  { key: 'parameters', value: JSON.stringify(parameters) },
                ],
              },
            },
          },
          {
            text: '👎 No',
            onClick: {
              action: {
                function: buttonClickHandlerUrl,
                parameters: [
                  { key: 'cardId', value: 'feedback_card' },
                  { key: 'action', value: 'no' },
                  { key: 'parameters', value: JSON.stringify(parameters) },
                ],
              },
            },
          },
        ],
      },
    });

    return {
      cardId: 'feedback_card',
      card: {
        header: {
          title: 'Was this response helpful?',
        },
        sections: [
          {
            widgets,
          },
        ],
      },
    };
  }

  /**
   * Build a generic HITL (Human-in-the-Loop) approval card for any tool interrupt.
   * Shows Approve + Reject buttons, and optionally a "Request Changes" button
   * when "edit" is in the tool's allowed_decisions. Risk-scored tools also get
   * "Always Allow" and "Allow Pattern" bypass buttons.
   */
  buildHitlCard(
    config: Config,
    toolName: string,
    reason: string,
    parameters: Record<string, string>,
    allowedDecisions: string[] = ['approve', 'reject'],
    actionRequests?: any[],
  ): chat_v1.Schema$CardWithId {
    const toolLabel = toolName.replace(/_/g, ' ');
    const buttonClickHandlerUrl = new URL(`/api/v1/chat/events`, config.baseUrl).toString();
    const editAllowed = allowedDecisions.includes('edit');
    const approveAllowed = allowedDecisions.includes('approve');

    // Extract proposed args for display
    const CONTENT_KEYS = ['content', 'body', 'description'];
    const HIDDEN_KEYS = ['reason', '_risk_metadata'];
    const firstAction = actionRequests?.[0];
    const toolArgs = firstAction?.args || {};
    const contentKey = CONTENT_KEYS.find((k: string) => k in toolArgs);
    const proposedContent = contentKey ? String(toolArgs[contentKey] || '') : '';
    const metaEntries = Object.entries(toolArgs).filter(
      ([k]) => !CONTENT_KEYS.includes(k) && !HIDDEN_KEYS.includes(k)
    );

    // Extract risk metadata for bypass buttons
    const riskMeta = toolArgs._risk_metadata as { source?: string; score?: number; threshold?: number; matched_pattern?: string | null } | undefined;
    const isRiskScored = riskMeta?.source === 'risk_score';

    const buttons: any[] = [
      {
        text: '✅ Approve',
        onClick: {
          action: {
            function: buttonClickHandlerUrl,
            parameters: [
              { key: 'cardId', value: 'hitl_card' },
              { key: 'action', value: 'approve' },
              { key: 'parameters', value: JSON.stringify(parameters) },
            ],
          },
        },
        color: {
          red: 0.0,
          green: 0.54,
          blue: 0.86,
          alpha: 1,
        },
      },
    ];

    if (editAllowed) {
      buttons.push({
        text: '✏️ Request Changes',
        onClick: {
          action: {
            function: buttonClickHandlerUrl,
            parameters: [
              { key: 'cardId', value: 'hitl_card' },
              { key: 'action', value: 'request_changes' },
              { key: 'parameters', value: JSON.stringify(parameters) },
            ],
          },
        },
      });
    }

    // Bypass buttons — only for risk-scored tools
    if (isRiskScored && approveAllowed) {
      if (riskMeta!.matched_pattern) {
        buttons.push({
          text: '🔓 Allow Pattern',
          onClick: {
            action: {
              function: buttonClickHandlerUrl,
              parameters: [
                { key: 'cardId', value: 'hitl_card' },
                { key: 'action', value: 'approve_bypass_pattern' },
                { key: 'parameters', value: JSON.stringify({ ...parameters, matchedPattern: riskMeta!.matched_pattern }) },
              ],
            },
          },
        });
      }
      buttons.push({
        text: '🔓 Always Allow',
        onClick: {
          action: {
            function: buttonClickHandlerUrl,
            parameters: [
              { key: 'cardId', value: 'hitl_card' },
              { key: 'action', value: 'approve_bypass_tool' },
              { key: 'parameters', value: JSON.stringify(parameters) },
            ],
          },
        },
      });
    }

    buttons.push({
      text: '❌ Reject',
      onClick: {
        action: {
          function: buttonClickHandlerUrl,
          parameters: [
            { key: 'cardId', value: 'hitl_card' },
            { key: 'action', value: 'reject' },
            { key: 'parameters', value: JSON.stringify(parameters) },
          ],
        },
      },
    });

    return {
      cardId: 'hitl_card',
      card: {
        header: {
          title: '⚠️ Approval Required',
          subtitle: toolLabel,
          imageType: 'CIRCLE',
        },
        sections: [
          {
            widgets: [
              {
                textParagraph: {
                  text: `<b>Reason:</b>\n${reason}`,
                },
              } as any,
              // Show risk score indicator for risk-scored tools
              ...(isRiskScored && riskMeta
                ? [
                    {
                      textParagraph: {
                        text: (() => {
                          const pct = Math.round((riskMeta.score ?? 0) * 100);
                          const riskLabel = pct >= 90 ? 'Critical' : pct >= 80 ? 'High' : pct >= 60 ? 'Medium' : 'Low';
                          let riskText = `🛡️ <b>Risk:</b> ${riskLabel} (${pct}%)`;
                          if (riskMeta.matched_pattern) {
                            riskText += ` — matched: <code>${riskMeta.matched_pattern}</code>`;
                          }
                          return riskText;
                        })(),
                      },
                    } as any,
                  ]
                : []),
              // Show metadata fields (name, skill_name, visibility, etc.)
              ...(metaEntries.length > 0
                ? [
                    {
                      textParagraph: {
                        text: metaEntries
                          .map(([k, v]) => `<b>${k}:</b> ${String(v).substring(0, 200)}`)
                          .join('\n'),
                      },
                    } as any,
                  ]
                : []),
              // Show proposed content preview (truncated)
              ...(proposedContent
                ? [
                    {
                      textParagraph: {
                        text: `<b>Proposed content:</b>\n<code>${proposedContent.substring(0, 2000)}</code>`,
                      },
                    } as any,
                  ]
                : []),
              {
                divider: {},
              },
              {
                buttonList: {
                  buttons,
                },
              } as any,
            ],
          },
        ],
      },
    };
  }

  /**
   * Build a multi-action HITL approval card for interrupts carrying more than one
   * action_request (e.g. parallel tool calls). Renders the detail of EVERY call (so
   * nothing is approved unseen) with a per-call Approve/Reject radio. "Submit
   * decisions" sends one decision per call (batched via formInputs), each echoing its
   * call_id so the server aligns by id. "Approve all"/"Reject all" send a blanket
   * decision (the server replicates it) for the common case.
   */
  buildMultiHitlCard(
    config: Config,
    parameters: Record<string, unknown>,
    actionRequests: any[],
  ): chat_v1.Schema$CardWithId {
    const buttonClickHandlerUrl = new URL(`/api/v1/chat/events`, config.baseUrl).toString();
    const CONTENT_KEYS = ['content', 'body', 'description'];
    const HIDDEN_KEYS = ['reason', '_risk_metadata'];

    const callIds = actionRequests.map((a) => a?.args?._risk_metadata?.call_id);
    const params = { ...parameters, callIds };
    const paramsJson = JSON.stringify(params);

    const widgets: any[] = [];
    actionRequests.forEach((action, i) => {
      const args = action?.args || {};
      const toolLabel = String(action?.name || 'unknown').replace(/_/g, ' ');
      const reason = String((args.description ?? args.reason) || '');
      const riskMeta = args._risk_metadata as { source?: string; score?: number; matched_pattern?: string | null } | undefined;
      const isRiskScored = riskMeta?.source === 'risk_score';
      const contentKey = CONTENT_KEYS.find((k: string) => k in args);
      const proposedContent = contentKey ? String(args[contentKey] || '') : '';
      const metaEntries = Object.entries(args).filter(([k]) => !CONTENT_KEYS.includes(k) && !HIDDEN_KEYS.includes(k));

      if (i > 0) widgets.push({ divider: {} });
      widgets.push({
        textParagraph: { text: `<b>${i + 1}. ${toolLabel}</b>${reason ? `\n${reason.substring(0, 1000)}` : ''}` },
      });
      if (isRiskScored && riskMeta) {
        const pct = Math.round((riskMeta.score ?? 0) * 100);
        const riskLabel = pct >= 90 ? 'Critical' : pct >= 80 ? 'High' : pct >= 60 ? 'Medium' : 'Low';
        let riskText = `🛡️ <b>Risk:</b> ${riskLabel} (${pct}%)`;
        if (riskMeta.matched_pattern) riskText += ` — matched: <code>${riskMeta.matched_pattern}</code>`;
        widgets.push({ textParagraph: { text: riskText } });
      }
      if (metaEntries.length > 0) {
        widgets.push({
          textParagraph: { text: metaEntries.map(([k, v]) => `<b>${k}:</b> ${String(v).substring(0, 200)}`).join('\n') },
        });
      }
      if (proposedContent) {
        widgets.push({ textParagraph: { text: `<b>Proposed content:</b>\n<code>${proposedContent.substring(0, 1500)}</code>` } });
      }
      widgets.push({
        selectionInput: {
          name: `decision_${i}`,
          label: 'Decision',
          type: 'RADIO_BUTTON',
          items: [
            { text: 'Approve', value: 'approve', selected: true },
            { text: 'Reject', value: 'reject', selected: false },
          ],
        },
      });
    });

    const mkButton = (text: string, action: string, color?: any) => ({
      text,
      onClick: {
        action: {
          function: buttonClickHandlerUrl,
          parameters: [
            { key: 'cardId', value: 'hitl_multi_card' },
            { key: 'action', value: action },
            { key: 'parameters', value: paramsJson },
          ],
        },
      },
      ...(color ? { color } : {}),
    });

    widgets.push({ divider: {} });
    widgets.push({
      buttonList: {
        buttons: [
          mkButton('✅ Approve all', 'approve'),
          mkButton('❌ Reject all', 'reject'),
          mkButton('⚖️ Submit decisions', 'submit_multi', { red: 0.0, green: 0.54, blue: 0.86, alpha: 1 }),
        ],
      },
    });

    return {
      cardId: 'hitl_multi_card',
      card: {
        header: { title: '⚠️ Approval Required', subtitle: `${actionRequests.length} actions`, imageType: 'CIRCLE' },
        sections: [{ widgets }],
      },
    };
  }

  /**
   * Build a HITL feedback form card with a text input for the user to describe
   * what should be changed. Replaces the approval card when "Request Changes" is clicked.
   */
  buildHitlFeedbackCard(
    config: Config,
    toolName: string,
    parameters: Record<string, string>,
  ): chat_v1.Schema$CardWithId {
    const toolLabel = toolName.replace(/_/g, ' ');
    const buttonClickHandlerUrl = new URL(`/api/v1/chat/events`, config.baseUrl).toString();

    return {
      cardId: 'hitl_feedback_card',
      card: {
        header: {
          title: '✏️ Request Changes',
          subtitle: toolLabel,
          imageType: 'CIRCLE',
        },
        sections: [
          {
            widgets: [
              {
                textInput: {
                  label: 'What should be changed?',
                  type: 'MULTIPLE_LINE',
                  name: 'feedback',
                  hintText: 'e.g. "Make the description shorter" or "Change scope to group"',
                },
              } as any,
              {
                buttonList: {
                  buttons: [
                    {
                      text: 'Submit Feedback',
                      onClick: {
                        action: {
                          function: buttonClickHandlerUrl,
                          parameters: [
                            { key: 'cardId', value: 'hitl_feedback_card' },
                            { key: 'action', value: 'submit_feedback' },
                            { key: 'parameters', value: JSON.stringify(parameters) },
                          ],
                        },
                      },
                      color: {
                        red: 0.0,
                        green: 0.54,
                        blue: 0.86,
                        alpha: 1,
                      },
                    },
                    {
                      text: 'Cancel',
                      onClick: {
                        action: {
                          function: buttonClickHandlerUrl,
                          parameters: [
                            { key: 'cardId', value: 'hitl_feedback_card' },
                            { key: 'action', value: 'cancel' },
                            { key: 'parameters', value: JSON.stringify(parameters) },
                          ],
                        },
                      },
                    },
                  ],
                },
              } as any,
            ],
          },
        ],
      },
    };
  }

  /**
   * List messages in a space, optionally filtered by thread.
   * Uses the spaces.messages.list API:
   * https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/list
   *
   * @param projectId - GCP project number
   * @param spaceId - Space resource name (e.g. "spaces/AAAA")
   * @param threadId - Optional thread resource name to filter by (e.g. "spaces/AAAA/threads/BBBB")
   * @param pageSize - Maximum number of messages to return (default 100, max 1000)
   * @returns Array of messages in chronological order (oldest first)
   */
  async listMessages(
    projectId: string,
    spaceId: string,
    threadId?: string,
    pageSize: number = 100
  ): Promise<chat_v1.Schema$Message[]> {
    try {
      const parent = spaceId;
      const allMessages: chat_v1.Schema$Message[] = [];
      let pageToken: string | undefined;

      do {
        const response = await this.chatApis[projectId].spaces.messages.list({
          parent,
          pageSize: Math.min(pageSize - allMessages.length, 1000),
          filter: threadId ? `thread.name = "${threadId}"` : undefined,
          orderBy: 'createTime asc',
          pageToken,
        });

        if (response.data.messages) {
          allMessages.push(...response.data.messages);
        }
        pageToken = response.data.nextPageToken ?? undefined;
      } while (pageToken && allMessages.length < pageSize);

      logger.debug(`Listed ${allMessages.length} messages from ${parent}${threadId ? ` (thread: ${threadId})` : ''}`);
      return allMessages;
    } catch (error) {
      logger.error(`Failed to list messages from ${spaceId}: ${error}`);
      return [];
    }
  }

  /**
   * Find an existing direct message space with a user.
   * Uses the spaces.findDirectMessage API:
   * https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces/findDirectMessage
   *
   * With app authentication (chat.bot scope), returns the DM space between
   * the specified user and the calling Chat app.
   *
   * @param projectId - GCP project number
   * @param userId - User resource name, e.g. "users/123456789"
   * @returns The DM Space, or null if no DM exists (404)
   */
  async findDirectMessage(projectId: string, userId: string): Promise<chat_v1.Schema$Space | null> {
    try {
      const name = userId.startsWith('users/') ? userId : `users/${userId}`;
      const response = await this.chatApis[projectId].spaces.findDirectMessage({ name });
      return response.data;
    } catch (error: any) {
      // 404 means no DM space exists with this user
      if (error?.code === 404 || error?.status === 404) {
        logger.debug(`No DM space found for user ${userId}`);
        return null;
      }
      logger.error(`Failed to find direct message space for ${userId}: ${error}`);
      return null;
    }
  }

  /**
   * Check if a space is a DM (direct message) space
   */
  isDmSpace(spaceType: string | null | undefined): boolean {
    return spaceType === 'DIRECT_MESSAGE';
  }
}
