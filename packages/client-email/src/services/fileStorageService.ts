import { randomUUID } from 'crypto';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { IObjectStorageService, createObjectStorageService, type StoredObject } from './objectStorageService.js';

const logger = Logger.getLogger('FileStorageService');

/**
 * Uploaded file result with storage key and URI
 */
export interface UploadedFile {
  key: string;
  s3Uri: string; // s3://bucket/key or file://bucket/key format - A2A server has permissions to download
  mimeType: string;
  name: string;
  size: number;
}

/**
 * Service for storing files via IObjectStorageService abstraction.
 * Supports S3, S3-compatible (MinIO), and local filesystem backends.
 */
export class FileStorageService {
  private readonly storage: IObjectStorageService;
  private readonly bucket: string;

  constructor(config: Config, storage?: IObjectStorageService) {
    this.storage = storage || createObjectStorageService({
      region: config.aws.region,
    });
    this.bucket = config.aws.s3.fileUploadBucket;

    logger.debug(`FileStorageService initialized with bucket: ${this.bucket}, backend: ${this.storage.storageType}`);
  }

  /**
   * Generate a unique storage key for a file
   * Format: email-client/{userId}/{contextId}/{uploadId}-{filename}
   */
  private generateKey(userId: string, contextId: string, fileName: string): string {
    const timestamp = Date.now();
    const uploadId = `${timestamp}-${randomUUID().substring(0, 8)}`;
    const sanitizedFileName = fileName.replace(/[^a-zA-Z0-9._-]/g, '_');
    const sanitizedContextId = contextId.replace(/[^a-zA-Z0-9._-]/g, '_');
    return `email-client/${userId}/${sanitizedContextId}/${uploadId}-${sanitizedFileName}`;
  }

  /**
   * Upload a file to storage and return the storage URI.
   * A2A server has permissions to download files directly.
   */
  async uploadFile(
    fileBuffer: Buffer,
    fileName: string,
    mimeType: string,
    userId: string,
    contextId: string
  ): Promise<UploadedFile> {
    const key = this.generateKey(userId, contextId, fileName);

    logger.info(`Uploading file: ${fileName} -> ${key} (${mimeType}, ${fileBuffer.length} bytes)`);

    try {
      const stored = await this.storage.upload(
        this.bucket,
        key,
        fileBuffer,
        {
          'original-filename': fileName,
          'sender-email': userId,
          'context-id': contextId,
          'upload-timestamp': new Date().toISOString(),
        },
        mimeType,
      );

      logger.info(`File uploaded: ${stored.uri}`);

      return {
        key,
        s3Uri: stored.uri,
        mimeType,
        name: fileName,
        size: fileBuffer.length,
      };
    } catch (error) {
      logger.error(`Failed to upload file ${fileName}: ${error}`);
      throw new Error(`Failed to upload file: ${error}`);
    }
  }

  /**
   * Get the bucket name
   */
  getBucket(): string {
    return this.bucket;
  }
}
