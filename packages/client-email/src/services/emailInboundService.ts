import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3';
import { SNSClient, SubscribeCommand, ListSubscriptionsByTopicCommand } from '@aws-sdk/client-sns';
import { simpleParser, ParsedMail, Attachment } from 'mailparser';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { Storage } from '../storage/storage.js';
import { UserAuthService } from '../services/userAuthService.js';
import { A2AClientService, A2ARequest } from '../services/a2aClientService.js';
import { FileStorageService } from '../services/fileStorageService.js';
import { EmailOutboundService, REPLY_MARKER } from '../services/emailOutboundService.js';
import { isSupportedFileType, isFileSizeAllowed } from '../utils/fileUtils.js';
import { randomUUID } from 'crypto';
import * as oidcModule from 'openid-client';

const logger = Logger.getLogger('EmailInboundService');

/**
 * Strip quoted reply history from an email body.
 * Looks for the REPLY_MARKER we inject in every outgoing email and
 * discards everything at and below it. Falls back to common email
 * quoting patterns (e.g. "On ... wrote:") when the marker is absent.
 */
function stripQuotedReply(body: string): string {
  // 1. Try our own marker first (most reliable)
  const markerIdx = body.indexOf(REPLY_MARKER);
  if (markerIdx !== -1) {
    return body.substring(0, markerIdx).trimEnd();
  }

  // 2. Fallback: common "On <date> <sender> wrote:" pattern
  const onWroteRegex = /\r?\n\s*On .+wrote:\s*\r?\n/;
  const onWroteMatch = onWroteRegex.exec(body);
  if (onWroteMatch) {
    return body.substring(0, onWroteMatch.index).trimEnd();
  }

  // 3. Fallback: Gmail-style "---------- Forwarded message ----------"
  const fwdIdx = body.indexOf('---------- Forwarded message ----------');
  if (fwdIdx !== -1) {
    return body.substring(0, fwdIdx).trimEnd();
  }

  return body;
}

/**
 * Parsed inbound email data
 */
export interface InboundEmail {
  senderEmail: string;
  subject: string;
  bodyText: string;
  messageId: string;
  attachments: Attachment[];
}

/**
 * S3 event notification payload (via SNS, triggered by S3 ObjectCreated)
 */
interface S3EventNotification {
  Records: Array<{
    eventSource: string;
    eventName: string;
    s3: {
      bucket: {
        name: string;
      };
      object: {
        key: string;
        size: number;
      };
    };
  }>;
}

/**
 * SNS message wrapper
 */
interface SNSMessage {
  Type: string;
  MessageId: string;
  TopicArn: string;
  Subject?: string;
  Message: string;
  SubscribeURL?: string;
  Token?: string;
  Timestamp: string;
}

/**
 * Handles inbound email processing:
 * - Receives SNS notifications triggered by S3 ObjectCreated events
 * - Parses raw email from S3
 * - Checks auth, uploads attachments, dispatches to A2A
 */
export class EmailInboundService {
  private readonly s3Client: S3Client;
  private readonly snsClient: SNSClient;
  private readonly config: Config;
  private readonly storage: Storage;
  private readonly userAuthService: UserAuthService;
  private readonly a2aClientService: A2AClientService;
  private readonly fileStorageService: FileStorageService;
  private readonly emailOutboundService: EmailOutboundService;

  constructor(
    config: Config,
    storage: Storage,
    userAuthService: UserAuthService,
    a2aClientService: A2AClientService,
    fileStorageService: FileStorageService,
    emailOutboundService: EmailOutboundService
  ) {
    this.s3Client = new S3Client({ region: config.aws.region });
    this.snsClient = new SNSClient({ region: config.aws.region });
    this.config = config;
    this.storage = storage;
    this.userAuthService = userAuthService;
    this.a2aClientService = a2aClientService;
    this.fileStorageService = fileStorageService;
    this.emailOutboundService = emailOutboundService;
  }

  /**
   * Idempotently ensure an HTTPS subscription exists for the inbound-email
   * SNS topic. Safe to call on every startup – if the subscription already
   * exists the AWS API simply returns the existing ARN.
   */
  async ensureSnsSubscription(): Promise<void> {
    const topicArn = this.config.sns.topicArn;
    if (!topicArn) {
      logger.warn('SNS_INBOUND_TOPIC_ARN not configured – skipping SNS subscription');
      return;
    }

    const endpoint = `${this.config.baseUrl}/api/v1/email/incoming`;

    // Check if a confirmed subscription already exists for this endpoint
    try {
      let nextToken: string | undefined;
      do {
        const listRes = await this.snsClient.send(
          new ListSubscriptionsByTopicCommand({ TopicArn: topicArn, NextToken: nextToken })
        );
        const existing = listRes.Subscriptions?.find(
          (s) => s.Protocol === 'https' && s.Endpoint === endpoint && s.SubscriptionArn !== 'PendingConfirmation'
        );
        if (existing) {
          logger.info(`SNS subscription already active: ${existing.SubscriptionArn}`);
          return;
        }
        nextToken = listRes.NextToken;
      } while (nextToken);
    } catch (err) {
      logger.warn(err, 'Could not list existing SNS subscriptions – will attempt subscribe anyway');
    }

    // Subscribe (idempotent on AWS side for the same topic+protocol+endpoint)
    try {
      const res = await this.snsClient.send(
        new SubscribeCommand({
          TopicArn: topicArn,
          Protocol: 'https',
          Endpoint: endpoint,
          ReturnSubscriptionArn: true,
        })
      );
      logger.info(`SNS subscribe requested – arn=${res.SubscriptionArn}`);
    } catch (err) {
      logger.error(err, 'Failed to subscribe to SNS topic');
      throw err;
    }
  }

  /**
   * Handle an incoming SNS message (from POST /api/v1/email/incoming).
   * Handles SubscriptionConfirmation and Notification types.
   */
  async handleSNSMessage(body: string): Promise<{ status: number; message: string }> {
    let snsMessage: SNSMessage;
    try {
      snsMessage = JSON.parse(body);
    } catch {
      logger.error('Failed to parse SNS message body');
      return { status: 400, message: 'Invalid JSON' };
    }

    // Handle subscription confirmation
    if (snsMessage.Type === 'SubscriptionConfirmation') {
      return this.handleSubscriptionConfirmation(snsMessage);
    }

    // Handle notification
    if (snsMessage.Type === 'Notification') {
      return this.handleNotification(snsMessage);
    }

    logger.warn(`Unknown SNS message type: ${snsMessage.Type}`);
    return { status: 400, message: `Unknown message type: ${snsMessage.Type}` };
  }

  /**
   * Auto-confirm SNS subscription by fetching the SubscribeURL.
   */
  private async handleSubscriptionConfirmation(msg: SNSMessage): Promise<{ status: number; message: string }> {
    if (!msg.SubscribeURL) {
      logger.error('Missing SubscribeURL in subscription confirmation');
      return { status: 400, message: 'Missing SubscribeURL' };
    }

    logger.info(`Confirming SNS subscription: ${msg.TopicArn}`);
    try {
      const result = await fetch(msg.SubscribeURL);
      if (!result.ok) {
        logger.error(`Failed to confirm SNS subscription, status ${result.status}: ${await result.text()}`);
        return { status: 500, message: 'Failed to confirm subscription' };
      }
      logger.info('SNS subscription confirmed');
      return { status: 200, message: 'Subscription confirmed' };
    } catch (error) {
      logger.error(error, 'Failed to confirm SNS subscription');
      return { status: 500, message: 'Failed to confirm subscription' };
    }
  }

  /**
   * Handle an S3 event notification: fetch raw email from S3, parse, and process.
   */
  private async handleNotification(msg: SNSMessage): Promise<{ status: number; message: string }> {
    let s3Event: S3EventNotification;
    try {
      s3Event = JSON.parse(msg.Message);
    } catch {
      logger.error('Failed to parse S3 event notification from SNS message');
      return { status: 400, message: 'Invalid S3 event notification' };
    }

    if (!s3Event.Records?.length) {
      logger.debug('No records in S3 event notification');
      return { status: 200, message: 'No records' };
    }

    for (const record of s3Event.Records) {
      const bucketName = record.s3.bucket.name;
      const objectKey = decodeURIComponent(record.s3.object.key.replace(/\+/g, ' '));
      logger.info(`Processing inbound email: bucket=${bucketName}, key=${objectKey}`);

      // Idempotency guard: claim this email for processing.
      // If another invocation (e.g. SNS retry) already claimed it, skip.
      const claimed = await this.storage.tryClaimEmail(objectKey, msg.MessageId);
      if (!claimed) {
        logger.warn(`Email already being processed (duplicate SNS delivery), skipping: key=${objectKey}`);
        continue;
      }

      try {
        // Fetch raw email from S3
        const rawEmail = await this.fetchRawEmailFromS3(bucketName, objectKey);

        // Parse the MIME email
        const parsed = await this.parseEmail(rawEmail);

        // Process the email through A2A pipeline
        await this.processInboundEmail(parsed, objectKey);

        // Mark as completed after successful processing
        await this.storage.markEmailCompleted(objectKey);
      } catch (error) {
        logger.error(error, `Failed to process inbound email: ${error}`);
        // Mark as failed so a subsequent SNS retry can re-claim it
        await this.storage
          .markEmailFailed(objectKey)
          .catch((e) => logger.error(e, `Failed to mark email as failed: key=${objectKey}`));
        return { status: 500, message: 'Failed to process email' };
      }
    }

    return { status: 200, message: 'Email processed' };
  }

  /**
   * Fetch raw MIME email from S3 (where SES stored it).
   */
  private async fetchRawEmailFromS3(bucket: string, key: string): Promise<Buffer> {
    const command = new GetObjectCommand({ Bucket: bucket, Key: key });
    const response = await this.s3Client.send(command);
    const bodyBytes = await response.Body?.transformToByteArray();
    if (!bodyBytes) {
      throw new Error('Empty email body from S3');
    }
    return Buffer.from(bodyBytes);
  }

  /**
   * Parse a raw MIME email into structured data.
   */
  private async parseEmail(rawEmail: Buffer): Promise<InboundEmail> {
    const parsed: ParsedMail = await simpleParser(rawEmail);

    const senderEmail = parsed.from?.value?.[0]?.address;
    if (!senderEmail) {
      throw new Error('Could not determine sender email address');
    }

    // Strip quoted reply history below our marker
    const rawText = parsed.text || '';
    const bodyText = stripQuotedReply(rawText);

    return {
      senderEmail: senderEmail.toLowerCase(),
      subject: parsed.subject || '(no subject)',
      bodyText,
      messageId: parsed.messageId || `<${randomUUID()}@email-client>`,
      attachments: parsed.attachments || [],
    };
  }

  /**
   * Main processing pipeline for an inbound email.
   * @param s3ObjectKey - Optional S3 key of the raw email (for diagnostics/tracking on inflight tasks)
   */
  async processInboundEmail(email: InboundEmail, s3ObjectKey?: string): Promise<void> {
    const { senderEmail, subject, bodyText, messageId, attachments } = email;
    logger.info(`Processing email from ${senderEmail}: "${subject}"`);

    // Check if user is authorized
    const isAuthorized = await this.userAuthService.isUserAuthorized(senderEmail);

    if (!isAuthorized) {
      logger.info(`User ${senderEmail} not authorized, sending auth prompt`);
      await this.handleUnauthenticatedUser(email);
      return;
    }

    // Get orchestrator access token
    const accessToken = await this.userAuthService.getOrchestratorToken(senderEmail);
    if (!accessToken) {
      logger.error(`Failed to get orchestrator token for ${senderEmail}`);
      await this.emailOutboundService.sendErrorNotification({
        to: senderEmail,
        subject,
        errorMessage: 'Failed to obtain access token. Please try authorizing again by sending a new email.',
        originalMessageId: messageId,
      });
      return;
    }

    // Resolve context (existing conversation or new)
    const contextKey = Storage.buildContextKey(senderEmail, subject);
    const existingContext = await this.storage.getContext(contextKey);
    const contextId = existingContext?.contextId;

    // Upload attachments to S3
    const fileUrls = await this.uploadAttachments(attachments, senderEmail, contextId || contextKey);

    // Build A2A request
    const webhookToken = randomUUID();
    const webhookUrl = `${this.config.baseUrl}/api/v1/a2a/callback`;

    const a2aRequest: A2ARequest = {
      senderEmail,
      subject,
      text: bodyText,
      fileUrls: fileUrls.length > 0 ? fileUrls : undefined,
      contextId,
      webhookUrl,
      webhookToken,
    };

    // --- Persist-first: save in-flight task with a placeholder ID BEFORE dispatching ---
    // This ensures that if the app crashes after A2A dispatch but before DB save,
    // the task recovery system can still find and recover the task.
    const placeholderTaskId = randomUUID();
    await this.storage.saveInFlightTask({
      taskId: placeholderTaskId,
      contextKey,
      contextId: contextId,
      senderEmail,
      subject,
      originalMessageId: messageId,
      webhookToken,
      s3ObjectKey,
    });
    logger.info(`Saved placeholder in-flight task: ${placeholderTaskId}`);

    // Send async request to A2A server
    logger.info(`Sending async A2A request for ${senderEmail}, contextId=${contextId || 'new'}`);
    const response = await this.a2aClientService.sendMessageAsync(a2aRequest, accessToken);

    if (!response.success) {
      // Clean up the placeholder task since A2A dispatch failed
      await this.storage.closeInFlightTask(placeholderTaskId);
      logger.error(`A2A request failed: ${response.error}`);
      await this.emailOutboundService.sendErrorNotification({
        to: senderEmail,
        subject,
        errorMessage: response.error || 'Failed to process your request.',
        originalMessageId: messageId,
      });
      return;
    }

    // Store context for conversation continuity
    if (response.contextId) {
      await this.storage.setContext(contextKey, response.contextId, {
        taskId: response.taskId,
        subject,
        senderEmail,
        originalMessageId: messageId,
      });
    }

    // Update the placeholder in-flight task with the real A2A task ID
    if (response.taskId) {
      await this.storage.updateInFlightTaskId(placeholderTaskId, response.taskId);
      logger.info(`In-flight task updated: ${placeholderTaskId} -> ${response.taskId}`);
    } else {
      // No task ID returned — clean up placeholder
      await this.storage.closeInFlightTask(placeholderTaskId);
      logger.warn('A2A response had no taskId, cleaned up placeholder in-flight task');
    }
  }

  /**
   * Handle an email from an unauthenticated user:
   * store as pending request and send auth prompt.
   */
  private async handleUnauthenticatedUser(email: InboundEmail): Promise<void> {
    const { senderEmail, subject, bodyText, messageId, attachments } = email;

    // Upload attachments to S3 before storing pending request
    const attachmentKeys: string[] = [];
    for (const att of attachments) {
      if (isSupportedFileType(att.contentType) && isFileSizeAllowed(att.size)) {
        try {
          const uploaded = await this.fileStorageService.uploadFile(
            att.content,
            att.filename || 'attachment',
            att.contentType,
            senderEmail,
            'pending'
          );
          attachmentKeys.push(uploaded.key);
        } catch (error) {
          logger.error(`Failed to upload pending attachment: ${error}`);
        }
      }
    }

    // Store the pending request
    await this.storage.savePendingRequest({
      email: senderEmail,
      subject,
      bodyText,
      originalMessageId: messageId,
      attachmentKeys: attachmentKeys.length > 0 ? attachmentKeys : undefined,
      status: 'pending',
    });

    // Generate PKCE state + verifier and build auth URL
    const state = randomUUID();
    const codeVerifier = oidcModule.randomPKCECodeVerifier();

    await this.userAuthService.storeAuthState(state, senderEmail, codeVerifier);

    const authUrl = await this.userAuthService.getAuthorizationUrl(state, codeVerifier);

    // Send auth prompt email
    await this.emailOutboundService.sendAuthPrompt({
      to: senderEmail,
      subject,
      authUrl,
      originalMessageId: messageId,
    });
  }

  /**
   * Upload email attachments to S3.
   */
  private async uploadAttachments(
    attachments: Attachment[],
    senderEmail: string,
    contextId: string
  ): Promise<Array<{ name: string; mimeType: string; url: string }>> {
    const results: Array<{ name: string; mimeType: string; url: string }> = [];

    for (const att of attachments) {
      if (!isSupportedFileType(att.contentType)) {
        logger.debug(`Skipping unsupported attachment type: ${att.contentType}`);
        continue;
      }
      if (!isFileSizeAllowed(att.size)) {
        logger.debug(`Skipping oversized attachment: ${att.size} bytes`);
        continue;
      }

      try {
        const uploaded = await this.fileStorageService.uploadFile(
          att.content,
          att.filename || 'attachment',
          att.contentType,
          senderEmail,
          contextId
        );
        results.push({
          name: att.filename || 'attachment',
          mimeType: att.contentType,
          url: uploaded.s3Uri,
        });
        logger.info(`Uploaded attachment: ${att.filename} -> ${uploaded.s3Uri}`);
      } catch (error) {
        logger.error(`Failed to upload attachment ${att.filename}: ${error}`);
      }
    }

    return results;
  }
}
