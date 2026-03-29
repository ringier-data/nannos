import { SQSClient, ReceiveMessageCommand, DeleteMessageCommand } from '@aws-sdk/client-sqs';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { EmailInboundService } from './emailInboundService.js';

const logger = Logger.getLogger('LocalSqsPoller');

/**
 * Polls an SQS queue for SNS notifications and feeds them into the
 * EmailInboundService pipeline. Only intended for local development
 * (ENVIRONMENT=local) so developers can receive real inbound emails
 * without exposing a public endpoint.
 *
 * The SQS queue is subscribed (via CloudFormation) to the
 * LocalInboundEmailSnsTopic which fires on S3 ObjectCreated events
 * under the `local-inbound-emails/` prefix.
 */
export class LocalSqsPoller {
  private readonly sqsClient: SQSClient;
  private readonly queueUrl: string;
  private readonly pollIntervalMs: number;
  private readonly emailInboundService: EmailInboundService;
  private running = false;
  private timeoutHandle: ReturnType<typeof setTimeout> | null = null;

  constructor(config: Config, emailInboundService: EmailInboundService) {
    this.sqsClient = new SQSClient({ region: config.aws.region });
    this.queueUrl = config.localSqs.queueUrl;
    this.pollIntervalMs = config.localSqs.pollIntervalMs;
    this.emailInboundService = emailInboundService;
  }

  /**
   * Start the polling loop. Safe to call multiple times – subsequent
   * calls are no-ops while the poller is already running.
   */
  start(): void {
    if (this.running) return;
    if (!this.queueUrl) {
      logger.warn('LOCAL_SQS_QUEUE_URL not set – local SQS poller disabled');
      return;
    }

    this.running = true;
    logger.info(`Local SQS poller started – queue=${this.queueUrl}, interval=${this.pollIntervalMs}ms`);
    this.poll();
  }

  /**
   * Stop the polling loop gracefully.
   */
  stop(): void {
    this.running = false;
    if (this.timeoutHandle) {
      clearTimeout(this.timeoutHandle);
      this.timeoutHandle = null;
    }
    logger.info('Local SQS poller stopped');
  }

  // ------------------------------------------------------------------ //

  private async poll(): Promise<void> {
    if (!this.running) return;

    try {
      const response = await this.sqsClient.send(
        new ReceiveMessageCommand({
          QueueUrl: this.queueUrl,
          MaxNumberOfMessages: 10,
          WaitTimeSeconds: 10, // long-poll
        })
      );

      const messages = response.Messages ?? [];
      if (messages.length > 0) {
        logger.info(`Received ${messages.length} message(s) from local SQS queue`);
      }

      for (const msg of messages) {
        if (!msg.Body) continue;

        try {
          // SQS wraps the SNS message in its own envelope.
          // The SNS JSON is the value the app already knows how to handle.
          const snsEnvelope = JSON.parse(msg.Body);

          // Forward straight to the existing handler
          const result = await this.emailInboundService.handleSNSMessage(JSON.stringify(snsEnvelope));

          logger.info(`Processed SQS message ${msg.MessageId} → status=${result.status}, msg=${result.message}`);
        } catch (err) {
          logger.error(err, `Failed to process SQS message ${msg.MessageId}`);
        }

        // Always delete the message (it's a dev queue with 1h retention –
        // no point in retrying stale messages).
        try {
          await this.sqsClient.send(
            new DeleteMessageCommand({
              QueueUrl: this.queueUrl,
              ReceiptHandle: msg.ReceiptHandle!,
            })
          );
        } catch (err) {
          logger.error(err, `Failed to delete SQS message ${msg.MessageId}`);
        }
      }
    } catch (err) {
      logger.error(err, 'Error polling local SQS queue');
    }

    // Schedule next poll
    if (this.running) {
      this.timeoutHandle = setTimeout(() => this.poll(), this.pollIntervalMs);
    }
  }
}
