import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3';
import { SNSClient, SubscribeCommand, ListSubscriptionsByTopicCommand } from '@aws-sdk/client-sns';
import { simpleParser, ParsedMail, Attachment } from 'mailparser';
import { Task, TaskStatusUpdateEvent, TaskArtifactUpdateEvent, TextPart, DataPart, FilePart, Artifact, Part } from '@a2a-js/sdk';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { Storage } from '../storage/storage.js';
import type { IUserAuthService } from '../services/userAuthService.js';
import { A2AClientService, A2ARequest, A2AArtifact, A2APart } from '../services/a2aClientService.js';
import { FileStorageService } from '../services/fileStorageService.js';
import { EmailOutboundService, REPLY_MARKER } from '../services/emailOutboundService.js';
import { isFinalState, isInterruptedState, getStateMessage } from '../utils/a2aWebhookHandler.js';
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
  private readonly userAuthService: IUserAuthService;
  private readonly a2aClientService: A2AClientService;
  private readonly fileStorageService: FileStorageService;
  private readonly emailOutboundService: EmailOutboundService;

  constructor(
    config: Config,
    storage: Storage,
    userAuthService: IUserAuthService,
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
   * Uses streaming A2A communication for immediate response delivery.
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

    // Get orchestrator access token. A throw is a failure that signing in again does not
    // fix (e.g. the token broker is down), so the sender is asked to try again later.
    let accessToken: string | null;
    try {
      accessToken = await this.userAuthService.getOrchestratorToken(senderEmail);
    } catch (error) {
      logger.error(error, `Could not get an orchestrator token for ${senderEmail}: ${error}`);
      await this.emailOutboundService.sendErrorNotification({
        to: senderEmail,
        subject,
        errorMessage: 'Nannos cannot process your email right now. Please send it again later.',
        originalMessageId: messageId,
      });
      return;
    }
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
    const existingContextId = existingContext?.contextId;

    // Upload attachments to S3
    const fileUrls = await this.uploadAttachments(attachments, senderEmail, existingContextId || contextKey);

    // Build A2A request (no webhook needed — using streaming)
    const a2aRequest: A2ARequest = {
      senderEmail,
      subject,
      text: bodyText,
      fileUrls: fileUrls.length > 0 ? fileUrls : undefined,
      contextId: existingContextId,
    };

    // --- Stream A2A request and process events ---
    logger.info(`Sending streaming A2A request for ${senderEmail}, contextId=${existingContextId || 'new'}`);

    let accumulatedTask: Task | null = null;
    let accumulatedArtifacts: Artifact[] = [];
    let taskSavedToDb = false;

    try {
      for await (const event of this.a2aClientService.sendMessageStreamRaw(a2aRequest, accessToken)) {
        logger.debug(`Stream event: ${event.kind}`);

        if (event.kind === 'task') {
          // First event — save task to DB for crash recovery
          accumulatedTask = event as Task;
          logger.info(`Task created: id=${accumulatedTask.id}, contextId=${accumulatedTask.contextId}`);

          // Save in-flight task with real ID (for crash recovery mid-stream)
          await this.storage.saveInFlightTask({
            taskId: accumulatedTask.id,
            contextKey,
            contextId: accumulatedTask.contextId,
            senderEmail,
            subject,
            originalMessageId: messageId,
            s3ObjectKey,
          });
          taskSavedToDb = true;

          // Store context for conversation continuity
          if (accumulatedTask.contextId) {
            await this.storage.setContext(contextKey, accumulatedTask.contextId, {
              taskId: accumulatedTask.id,
              subject,
              senderEmail,
              originalMessageId: messageId,
            });
          }
        } else if (event.kind === 'status-update') {
          const statusEvent = event as TaskStatusUpdateEvent;
          const state = statusEvent.status?.state || 'working';
          logger.debug(`Status update: taskId=${statusEvent.taskId}, state=${state}`);

          // Update accumulated task status
          if (accumulatedTask) {
            accumulatedTask.status = statusEvent.status;
          }

          // Check for HITL interrupt and extract context for email
          if (state === 'input-required' && statusEvent.status?.message) {
            const hitlInfo = this.extractHitlInfo(statusEvent);
            if (hitlInfo) {
              logger.info(`HITL interrupt detected: ${hitlInfo.summary}`);
            }
          }
        } else if (event.kind === 'artifact-update') {
          const artifactEvent = event as TaskArtifactUpdateEvent;
          if (artifactEvent.artifact) {
            accumulatedArtifacts.push(artifactEvent.artifact);
            logger.debug(`Artifact received: ${artifactEvent.artifact.artifactId}`);

            // Update accumulated task artifacts
            if (accumulatedTask) {
              if (!accumulatedTask.artifacts) accumulatedTask.artifacts = [];
              accumulatedTask.artifacts.push(artifactEvent.artifact);
            }
          }
        } else if (event.kind === 'message') {
          // Direct message response (rare — most responses come as tasks)
          logger.debug(`Direct message received, contextId=${event.contextId}`);
        }
      }
    } catch (error) {
      logger.error(error, `A2A stream error for ${senderEmail}`);

      // Clean up in-flight task if we saved one
      if (taskSavedToDb && accumulatedTask) {
        await this.storage.closeInFlightTask(accumulatedTask.id).catch(() => {});
      }

      await this.emailOutboundService.sendErrorNotification({
        to: senderEmail,
        subject,
        errorMessage: 'An error occurred while processing your request. Please try again.',
        originalMessageId: messageId,
      });
      return;
    }

    // --- Stream completed — send reply email ---
    if (!accumulatedTask) {
      logger.error(`No task received from A2A server for ${senderEmail}`);
      await this.emailOutboundService.sendErrorNotification({
        to: senderEmail,
        subject,
        errorMessage: 'No response received from the server. Please try again.',
        originalMessageId: messageId,
      });
      return;
    }

    const finalState = accumulatedTask.status?.state as
      | 'completed'
      | 'working'
      | 'blocked'
      | 'failed'
      | 'submitted'
      | 'canceled'
      | 'rejected'
      | 'input-required'
      | 'auth-required'
      | undefined;
    logger.info(`Stream completed for task ${accumulatedTask.id}, state=${finalState}`);

    // Send reply for final or interrupted states
    if (isFinalState(finalState) || isInterruptedState(finalState)) {
      // Build response message
      let message = this.extractResponseMessage(accumulatedTask, accumulatedArtifacts);
      if (!message) {
        message = getStateMessage(finalState);
      }

      // Convert artifacts to our format for email attachments
      const artifacts = this.convertArtifactsForEmail(accumulatedArtifacts);

      await this.emailOutboundService.sendReply({
        to: senderEmail,
        subject: subject || 'A2A Response',
        message,
        artifacts,
        originalMessageId: messageId,
      });

      // Close in-flight task tracking
      await this.storage.closeInFlightTask(accumulatedTask.id);
      logger.info(`Task ${accumulatedTask.id} completed, sent reply and closed tracking`);
    } else {
      // Task still in progress (shouldn't happen with streaming, but handle gracefully)
      logger.warn(`Stream ended but task ${accumulatedTask.id} still in state ${finalState}`);
    }
  }

  /**
   * Extract HITL (human-in-the-loop) info from a status update for email formatting.
   */
  private extractHitlInfo(
    statusEvent: TaskStatusUpdateEvent
  ): { summary: string; actionRequests?: unknown[] } | null {
    if (!statusEvent.status?.message?.parts) return null;

    let summary = '';
    let actionRequests: unknown[] | undefined;

    for (const part of statusEvent.status.message.parts) {
      if (part.kind === 'text') {
        summary += (part as TextPart).text;
      } else if (part.kind === 'data') {
        const data = (part as DataPart).data as Record<string, unknown>;
        if (data?.action_requests) {
          actionRequests = data.action_requests as unknown[];
        }
      }
    }

    return summary ? { summary, actionRequests } : null;
  }

  /**
   * Extract response message text from task and artifacts.
   */
  private extractResponseMessage(task: Task, artifacts: Artifact[]): string {
    // First try to extract from artifacts
    const texts: string[] = [];
    for (const artifact of artifacts) {
      for (const part of artifact.parts) {
        if (part.kind === 'text') {
          texts.push((part as TextPart).text);
        }
      }
    }
    if (texts.length > 0) {
      return texts.join('\n\n');
    }

    // Fall back to status message
    if (task.status?.message?.parts) {
      for (const part of task.status.message.parts) {
        if (part.kind === 'text') {
          return (part as TextPart).text;
        }
      }
    }

    return '';
  }

  /**
   * Convert SDK Artifacts to A2AArtifact format for email service.
   */
  private convertArtifactsForEmail(artifacts: Artifact[]): A2AArtifact[] | undefined {
    if (artifacts.length === 0) return undefined;

    return artifacts.map((a) => ({
      artifactId: a.artifactId,
      name: a.name,
      parts: a.parts.map((p: Part): A2APart => {
        if (p.kind === 'text') {
          return { kind: p.kind, text: (p as TextPart).text };
        } else if (p.kind === 'file') {
          const filePart = p as FilePart;
          const fileData = filePart.file as { bytes?: string; mimeType?: string; name?: string; uri?: string };
          return {
            kind: p.kind,
            data: fileData.bytes,
            mimeType: fileData.mimeType,
            name: fileData.name,
            uri: fileData.uri,
          };
        } else if (p.kind === 'data') {
          const dataPart = p as DataPart;
          return {
            kind: p.kind,
            text: JSON.stringify(dataPart.data),
          };
        }
        return { kind: (p as Part).kind };
      }),
    }));
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
