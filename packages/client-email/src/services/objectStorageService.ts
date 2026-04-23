/**
 * Object storage abstraction layer for file operations.
 *
 * Provides a pluggable interface for object storage backends, enabling deployment
 * flexibility across AWS S3, S3-compatible APIs (MinIO, DigitalOcean Spaces, Wasabi),
 * and local filesystem storage for development.
 *
 * Design choice: Custom thin abstraction over libraries like apache-libcloud
 * to minimize dependencies. The interface is intentionally small — community
 * contributors can add backends (GCS, Azure Blob, etc.) by implementing
 * IObjectStorageService.
 */

import { S3Client, PutObjectCommand, GetObjectCommand, DeleteObjectCommand, ListObjectsV2Command } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { Logger } from '../utils/logger.js';
import * as fs from 'fs/promises';
import * as path from 'path';

const logger = Logger.getLogger('ObjectStorageService');

/**
 * Represents a stored object in any backend.
 */
export interface StoredObject {
  uri: string;      // Canonical URI (s3://bucket/key or file://bucket/key)
  bucket: string;
  key: string;
  name: string;     // Original filename (basename of key)
  mimeType: string;
  size: number;
}

/**
 * Abstract interface for object storage operations.
 */
export interface IObjectStorageService {
  /** Upload content to storage. */
  upload(
    bucket: string,
    key: string,
    content: Buffer,
    metadata?: Record<string, string>,
    contentType?: string,
  ): Promise<StoredObject>;

  /** Download object content from storage. */
  download(bucket: string, key: string): Promise<Buffer>;

  /** Generate a presigned/temporary access URL. */
  generatePresignedUrl(bucket: string, key: string, expirationSeconds?: number): Promise<string>;

  /** Delete an object from storage. */
  delete(bucket: string, key: string): Promise<void>;

  /** List object keys by prefix. */
  listObjects(bucket: string, prefix?: string): Promise<string[]>;

  /** Return storage type identifier ('s3' or 'local'). */
  readonly storageType: string;
}

/**
 * AWS S3 and S3-compatible backend implementation.
 *
 * Works with AWS S3 (default), MinIO, DigitalOcean Spaces, Wasabi, etc.
 * When endpoint is provided, explicit credentials are typically required.
 */
export class S3ObjectStorageService implements IObjectStorageService {
  private readonly s3Client: S3Client;
  readonly storageType = 's3';

  constructor(options: {
    region?: string;
    endpoint?: string;
    accessKeyId?: string;
    secretAccessKey?: string;
  } = {}) {
    const clientConfig: Record<string, unknown> = {
      region: options.region || process.env.AWS_REGION || 'eu-central-1',
    };

    if (options.endpoint) {
      clientConfig.endpoint = options.endpoint;
      clientConfig.forcePathStyle = true; // Required for MinIO and most S3-compatible services
    }

    if (options.accessKeyId && options.secretAccessKey) {
      clientConfig.credentials = {
        accessKeyId: options.accessKeyId,
        secretAccessKey: options.secretAccessKey,
      };
    }

    this.s3Client = new S3Client(clientConfig);
    logger.debug(`S3ObjectStorageService initialized (region: ${clientConfig.region}, endpoint: ${options.endpoint || 'default'})`);
  }

  async upload(
    bucket: string,
    key: string,
    content: Buffer,
    metadata?: Record<string, string>,
    contentType?: string,
  ): Promise<StoredObject> {
    const command = new PutObjectCommand({
      Bucket: bucket,
      Key: key,
      Body: content,
      ContentType: contentType || 'application/octet-stream',
      Metadata: metadata,
    });

    await this.s3Client.send(command);
    const name = key.includes('/') ? key.split('/').pop()! : key;

    logger.debug(`Uploaded ${content.length} bytes to s3://${bucket}/${key}`);

    return {
      uri: `s3://${bucket}/${key}`,
      bucket,
      key,
      name,
      mimeType: contentType || 'application/octet-stream',
      size: content.length,
    };
  }

  async download(bucket: string, key: string): Promise<Buffer> {
    const command = new GetObjectCommand({ Bucket: bucket, Key: key });
    const response = await this.s3Client.send(command);
    const bodyBytes = await response.Body?.transformToByteArray();
    if (!bodyBytes) {
      throw new Error(`Empty body from S3: s3://${bucket}/${key}`);
    }
    return Buffer.from(bodyBytes);
  }

  async generatePresignedUrl(bucket: string, key: string, expirationSeconds = 3600): Promise<string> {
    const clampedExpiration = Math.min(expirationSeconds, 86400);
    const command = new GetObjectCommand({ Bucket: bucket, Key: key });
    return getSignedUrl(this.s3Client, command, { expiresIn: clampedExpiration });
  }

  async delete(bucket: string, key: string): Promise<void> {
    const command = new DeleteObjectCommand({ Bucket: bucket, Key: key });
    await this.s3Client.send(command);
    logger.debug(`Deleted s3://${bucket}/${key}`);
  }

  async listObjects(bucket: string, prefix = ''): Promise<string[]> {
    const keys: string[] = [];
    let continuationToken: string | undefined;

    do {
      const command = new ListObjectsV2Command({
        Bucket: bucket,
        Prefix: prefix,
        ContinuationToken: continuationToken,
      });
      const response = await this.s3Client.send(command);
      for (const obj of response.Contents || []) {
        if (obj.Key) keys.push(obj.Key);
      }
      continuationToken = response.IsTruncated ? response.NextContinuationToken : undefined;
    } while (continuationToken);

    return keys;
  }
}

/**
 * Filesystem-based storage backend for local development.
 *
 * Files are stored under {rootPath}/{bucket}/{key}. Metadata is stored
 * in sidecar .meta.json files alongside each object.
 *
 * Presigned URLs are replaced with local API paths.
 */
export class LocalObjectStorageService implements IObjectStorageService {
  private readonly rootPath: string;
  private readonly baseUrl: string;
  readonly storageType = 'local';

  constructor(options: { rootPath?: string; baseUrl?: string } = {}) {
    this.rootPath = options.rootPath || process.env.LOCAL_STORAGE_PATH || './local-storage';
    this.baseUrl = (options.baseUrl || '/api/v1/files/local').replace(/\/+$/, '');
    logger.info(`Local object storage initialized at: ${path.resolve(this.rootPath)}`);
  }

  private objectPath(bucket: string, key: string): string {
    const resolved = path.resolve(this.rootPath, bucket, key);
    if (!resolved.startsWith(path.resolve(this.rootPath))) {
      throw new Error(`Path traversal detected: ${bucket}/${key}`);
    }
    return resolved;
  }

  private metaPath(objPath: string): string {
    const dir = path.dirname(objPath);
    const base = path.basename(objPath);
    return path.join(dir, `.${base}.meta.json`);
  }

  async upload(
    bucket: string,
    key: string,
    content: Buffer,
    metadata?: Record<string, string>,
    contentType?: string,
  ): Promise<StoredObject> {
    const objPath = this.objectPath(bucket, key);
    await fs.mkdir(path.dirname(objPath), { recursive: true });
    await fs.writeFile(objPath, content);

    // Write metadata sidecar
    const meta = {
      contentType: contentType || 'application/octet-stream',
      size: content.length,
      metadata: metadata || {},
    };
    await fs.writeFile(this.metaPath(objPath), JSON.stringify(meta));

    const name = key.includes('/') ? key.split('/').pop()! : key;
    logger.debug(`Uploaded ${content.length} bytes to local: ${objPath}`);

    return {
      uri: `file://${bucket}/${key}`,
      bucket,
      key,
      name,
      mimeType: contentType || 'application/octet-stream',
      size: content.length,
    };
  }

  async download(bucket: string, key: string): Promise<Buffer> {
    const objPath = this.objectPath(bucket, key);
    return fs.readFile(objPath);
  }

  async generatePresignedUrl(bucket: string, key: string, _expirationSeconds = 3600): Promise<string> {
    return `${this.baseUrl}/${key}`;
  }

  async delete(bucket: string, key: string): Promise<void> {
    const objPath = this.objectPath(bucket, key);
    try { await fs.unlink(objPath); } catch { /* ignore if not found */ }
    try { await fs.unlink(this.metaPath(objPath)); } catch { /* ignore */ }
    logger.debug(`Deleted local object: ${bucket}/${key}`);
  }

  async listObjects(bucket: string, prefix = ''): Promise<string[]> {
    const bucketPath = path.resolve(this.rootPath, bucket);
    const prefixPath = prefix ? path.join(bucketPath, prefix) : bucketPath;

    try {
      const stat = await fs.stat(prefixPath);
      if (stat.isFile()) {
        return [path.relative(bucketPath, prefixPath)];
      }
    } catch {
      return [];
    }

    const keys: string[] = [];
    const walk = async (dir: string) => {
      const entries = await fs.readdir(dir, { withFileTypes: true });
      for (const entry of entries) {
        const fullPath = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          await walk(fullPath);
        } else if (entry.isFile() && !entry.name.endsWith('.meta.json')) {
          const rel = path.relative(bucketPath, fullPath);
          if (!prefix || rel.startsWith(prefix)) {
            keys.push(rel);
          }
        }
      }
    };

    try {
      await walk(prefixPath);
    } catch {
      // Directory doesn't exist
    }
    return keys.sort();
  }
}

/**
 * Configuration for object storage service creation.
 */
export interface ObjectStorageConfig {
  type?: string;        // 's3' (default) or 'local'
  region?: string;
  endpoint?: string;    // S3-compatible endpoint URL
  accessKeyId?: string;
  secretAccessKey?: string;
  localPath?: string;
}

/**
 * Factory function to create an object storage service from configuration.
 */
export function createObjectStorageService(config?: ObjectStorageConfig): IObjectStorageService {
  const storageType = config?.type || process.env.OBJECT_STORAGE_TYPE || 's3';

  if (storageType === 'local') {
    return new LocalObjectStorageService({
      rootPath: config?.localPath || process.env.LOCAL_STORAGE_PATH,
    });
  }

  return new S3ObjectStorageService({
    region: config?.region || process.env.AWS_REGION,
    endpoint: config?.endpoint || process.env.S3_ENDPOINT_URL,
    accessKeyId: config?.accessKeyId || process.env.S3_ACCESS_KEY_ID,
    secretAccessKey: config?.secretAccessKey || process.env.S3_SECRET_ACCESS_KEY,
  });
}
