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
      scopes: [
        'https://www.googleapis.com/auth/chat.messages.readonly',
      ],
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
   * Send a card message to a Google Chat space
   */
  async sendCardMessage(
    projectId: string,
    spaceId: string,
    cardsV2: chat_v1.Schema$CardWithId[],
    threadId?: string
  ): Promise<chat_v1.Schema$Message> {
    return this.sendMessage({
      projectId,
      spaceId,
      cardsV2,
      threadId,
      messageReplyOption: threadId ? 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD' : undefined,
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
    attachmentMetadata: { resourceName: string; contentType: string; fileName: string },
  ): Promise<{
    data: Buffer;
    contentType: string;
    fileName: string;
  } | null> {
    // Try bot-level download first
    const botResult = await this.downloadAttachmentWithClient(
      this.chatApis[projectId],
      attachmentMetadata
    );
    if (botResult) return botResult;

    // Fallback: impersonate a user via domain-wide delegation.
    logger.info(`Bot cannot access attachment ${attachmentMetadata.resourceName}, retrying media.download with impersonation`);
    try {
        const userClient = this.createUserImpersonatedClient(projectId, userEmail);
        const botResult = await this.downloadAttachmentWithClient(
          userClient,
          attachmentMetadata
        );
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
    attachmentMetadata: { resourceName: string; contentType: string; fileName: string },
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
   * Build a status card for showing progress
   */
  buildStatusCard(status: string, detail?: string): chat_v1.Schema$CardWithId {
    const widgets: chat_v1.Schema$WidgetMarkup[] = [
      {
        decoratedText: {
          icon: { knownIcon: 'CLOCK' },
          topLabel: 'Status',
          text: status,
        },
      } as any,
    ];

    if (detail) {
      widgets.push({
        textParagraph: {
          text: detail,
        },
      } as any);
    }

    return {
      cardId: 'status-card',
      card: {
        sections: [{ widgets: widgets as any }],
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
