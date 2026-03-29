import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { randomUUID } from 'crypto';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';

const logger = Logger.getLogger('FileStorageService');

/**
 * Uploaded file result with S3 key and URI
 */
export interface UploadedFile {
  key: string;
  s3Uri: string; // s3://bucket/key format - A2A server has permissions to download
  mimeType: string;
  name: string;
  size: number;
}

/**
 * Service for storing files in S3
 * A2A server has permissions to download files directly from S3
 */
export class FileStorageService {
  private readonly s3Client: S3Client;
  private readonly bucket: string;

  constructor(config: Config) {
    this.s3Client = new S3Client({ region: config.aws.region });
    this.bucket = config.aws.s3.fileUploadBucket;

    logger.debug(`FileStorageService initialized with bucket: ${this.bucket}, region: ${config.aws.region}`);
  }

  /**
   * Generate a unique S3 key for a file
   * Format: /{userId}/{contextId}/{uploadId}/{filename}
   * - userId: Slack user ID
   * - contextId: Thread/conversation identifier (threadTs or messageTs)
   * - uploadId: Unique ID for this upload batch (timestamp + short UUID)
   */
  private generateS3Key(userId: string, contextId: string, fileName: string): string {
    const timestamp = Date.now();
    const uploadId = `${timestamp}-${randomUUID().substring(0, 8)}`;
    // Sanitize filename to remove problematic characters
    const sanitizedFileName = fileName.replace(/[^a-zA-Z0-9._-]/g, '_');
    // Sanitize contextId (thread timestamps have dots)
    const sanitizedContextId = contextId.replace(/[^a-zA-Z0-9._-]/g, '_');
    return `slack-client/${userId}/${sanitizedContextId}/${uploadId}-${sanitizedFileName}`;
  }

  /**
   * Upload a file to S3 and return the S3 URI
   * A2A server has permissions to download files directly from S3
   */
  async uploadFile(
    fileBuffer: Buffer,
    fileName: string,
    mimeType: string,
    userId: string,
    contextId: string
  ): Promise<UploadedFile> {
    const key = this.generateS3Key(userId, contextId, fileName);

    logger.info(`Uploading file to S3: ${fileName} -> ${key} (${mimeType}, ${fileBuffer.length} bytes)`);

    try {
      // Upload to S3
      const putCommand = new PutObjectCommand({
        Bucket: this.bucket,
        Key: key,
        Body: fileBuffer,
        ContentType: mimeType,
        Metadata: {
          'original-filename': fileName,
          'slack-user-id': userId,
          'context-id': contextId,
          'upload-timestamp': new Date().toISOString(),
        },
      });

      await this.s3Client.send(putCommand);
      logger.debug(`File uploaded successfully: ${key}`);

      // Build S3 URI (A2A server has permissions to download directly)
      const s3Uri = `s3://${this.bucket}/${key}`;

      logger.info(`File uploaded to S3: ${s3Uri}`);

      return {
        key,
        s3Uri,
        mimeType,
        name: fileName,
        size: fileBuffer.length,
      };
    } catch (error) {
      logger.error(`Failed to upload file ${fileName}: ${error}`);
      throw new Error(`Failed to upload file to S3: ${error}`);
    }
  }

  /**
   * Get the bucket name
   */
  getBucket(): string {
    return this.bucket;
  }
}
