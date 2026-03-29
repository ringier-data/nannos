import { Logger } from './logger.js';
import { FileStorageService, UploadedFile } from '../services/fileStorageService.js';

const logger = Logger.getLogger('fileUtils');

/**
 * Slack file object from event.files array
 */
export interface SlackFile {
  id: string;
  name: string;
  mimetype: string;
  filetype: string;
  size: number;
  url_private: string;
  url_private_download?: string;
  permalink?: string;
  thumb_360?: string;
  thumb_480?: string;
  thumb_720?: string;
  thumb_960?: string;
  thumb_1024?: string;
}

/**
 * Processed file ready for A2A
 */
export interface ProcessedFile {
  name: string;
  mimeType: string;
  data: string; // base64 encoded
  size: number;
}

/**
 * Supported file types for processing
 */
const SUPPORTED_MIME_TYPES = new Set([
  // Images
  'image/jpeg',
  'image/png',
  'image/gif',
  'image/webp',
  'image/svg+xml',
  // Documents
  'application/pdf',
  'text/plain',
  'text/csv',
  'text/html',
  'text/markdown',
  'application/json',
  'application/xml',
  // Office documents
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document', // docx
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', // xlsx
  'application/vnd.openxmlformats-officedocument.presentationml.presentation', // pptx
  'application/msword', // doc
  'application/vnd.ms-excel', // xls
  'application/vnd.ms-powerpoint', // ppt
  // Audio
  'audio/mpeg', // mp3
  'audio/mp4', // m4a
  'audio/x-m4a', // m4a (alternative MIME type)
  'audio/wav', // wav
  'audio/wave', // wav (alternative MIME type)
  'audio/x-wav', // wav (alternative MIME type)
  'audio/webm', // webm
  'audio/ogg', // ogg
  'audio/aac', // aac
  'audio/flac', // flac
]);

/**
 * Maximum file size to process (10MB)
 */
const MAX_FILE_SIZE = 10 * 1024 * 1024;

/**
 * Check if a file type is supported for processing
 */
export function isSupportedFileType(mimeType: string): boolean {
  return SUPPORTED_MIME_TYPES.has(mimeType);
}

/**
 * Check if a file is within size limits
 */
export function isFileSizeAllowed(size: number): boolean {
  return size <= MAX_FILE_SIZE;
}

/**
 * Download a file from Slack using the bot token
 */
export async function downloadSlackFile(fileUrl: string, botToken: string): Promise<Buffer> {
  logger.debug(`Downloading file from: ${fileUrl}`);

  // Validate bot token is present
  if (!botToken) {
    throw new Error('Bot token is missing - cannot download Slack file');
  }

  // Log token prefix for debugging (first 10 chars only for security)
  logger.debug(`Using bot token starting with: ${botToken.substring(0, 10)}...`);

  const response = await fetch(fileUrl, {
    headers: {
      Authorization: `Bearer ${botToken}`,
    },
    // Don't follow redirects - Slack redirects to login page if auth fails
    redirect: 'manual',
  });

  // Check for redirect (indicates auth failure - Slack redirects to login)
  if (response.status >= 300 && response.status < 400) {
    const location = response.headers.get('location');
    logger.error(`Slack file download redirected (auth failure). Status: ${response.status}, Location: ${location}`);
    throw new Error(`Slack file download failed: redirected to ${location} (check bot token permissions)`);
  }

  if (!response.ok) {
    throw new Error(`Failed to download file: ${response.status} ${response.statusText}`);
  }

  // Verify we got binary content, not HTML (login page)
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('text/html')) {
    logger.error(`Received HTML instead of file content. Content-Type: ${contentType}`);
    throw new Error('Slack file download returned HTML (likely auth failure or invalid URL)');
  }

  const arrayBuffer = await response.arrayBuffer();
  const buffer = Buffer.from(arrayBuffer);

  logger.debug(`Downloaded ${buffer.length} bytes, Content-Type: ${contentType}`);

  return buffer;
}

/**
 * Convert a buffer to base64 string
 */
export function bufferToBase64(buffer: Buffer): string {
  return buffer.toString('base64');
}

/**
 * Process a Slack file: download and convert to base64
 */
export async function processSlackFile(file: SlackFile, botToken: string): Promise<ProcessedFile | null> {
  // Check file type
  if (!isSupportedFileType(file.mimetype)) {
    logger.debug(`Unsupported file type: ${file.mimetype} for file ${file.name}`);
    return null;
  }

  // Check file size
  if (!isFileSizeAllowed(file.size)) {
    logger.debug(`File too large: ${file.size} bytes for file ${file.name}`);
    return null;
  }

  try {
    // Download the file
    const downloadUrl = file.url_private_download || file.url_private;
    const buffer = await downloadSlackFile(downloadUrl, botToken);

    // Convert to base64
    const base64Data = bufferToBase64(buffer);

    logger.info(`Successfully processed file: ${file.name} (${file.mimetype}, ${file.size} bytes)`);

    return {
      name: file.name,
      mimeType: file.mimetype,
      data: base64Data,
      size: file.size,
    };
  } catch (error) {
    logger.error(`Failed to process file ${file.name}: ${error}`);
    return null;
  }
}

/**
 * Process multiple Slack files
 */
export async function processSlackFiles(files: SlackFile[], botToken: string): Promise<ProcessedFile[]> {
  const results = await Promise.all(files.map((file) => processSlackFile(file, botToken)));

  // Filter out nulls (failed or unsupported files)
  return results.filter((f): f is ProcessedFile => f !== null);
}

/**
 * Get a human-readable description of file processing issues
 */
export function getFileProcessingWarnings(files: SlackFile[]): string[] {
  const warnings: string[] = [];

  for (const file of files) {
    if (!isSupportedFileType(file.mimetype)) {
      warnings.push(`• ${file.name}: Unsupported file type (${file.mimetype})`);
    } else if (!isFileSizeAllowed(file.size)) {
      warnings.push(`• ${file.name}: File too large (${(file.size / 1024 / 1024).toFixed(1)}MB, max 10MB)`);
    }
  }

  return warnings;
}

/**
 * Check if any files in the array are processable
 */
export function hasProcessableFiles(files: SlackFile[]): boolean {
  return files.some((f) => isSupportedFileType(f.mimetype) && isFileSizeAllowed(f.size));
}

/**
 * Decode base64 data to a buffer (for posting file artifacts)
 */
export function base64ToBuffer(base64: string): Buffer {
  return Buffer.from(base64, 'base64');
}

/**
 * Get file extension from mime type
 */
export function getExtensionFromMimeType(mimeType: string): string {
  const extensions: Record<string, string> = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/svg+xml': 'svg',
    'application/pdf': 'pdf',
    'text/plain': 'txt',
    'text/csv': 'csv',
    'text/html': 'html',
    'text/markdown': 'md',
    'application/json': 'json',
    'application/xml': 'xml',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
    'application/msword': 'doc',
    'application/vnd.ms-excel': 'xls',
    'application/vnd.ms-powerpoint': 'ppt',
    'audio/mpeg': 'mp3',
    'audio/mp4': 'm4a',
    'audio/x-m4a': 'm4a',
    'audio/wav': 'wav',
    'audio/wave': 'wav',
    'audio/x-wav': 'wav',
    'audio/webm': 'webm',
    'audio/ogg': 'ogg',
    'audio/aac': 'aac',
    'audio/flac': 'flac',
  };

  return extensions[mimeType] || 'bin';
}

/**
 * Processed file with S3 URI (for A2A FileWithUri)
 * A2A server has permissions to download files directly from S3
 */
export interface ProcessedFileWithUrl {
  name: string;
  mimeType: string;
  url: string; // S3 URI (s3://bucket/key)
  size: number;
}

/**
 * Process a Slack file: download, upload to S3, and return S3 URI
 */
export async function processSlackFileToS3(
  file: SlackFile,
  botToken: string,
  fileStorageService: FileStorageService,
  userId: string,
  contextId: string
): Promise<ProcessedFileWithUrl | null> {
  // Check file type
  if (!isSupportedFileType(file.mimetype)) {
    logger.debug(`Unsupported file type: ${file.mimetype} for file ${file.name}`);
    return null;
  }

  // Check file size
  if (!isFileSizeAllowed(file.size)) {
    logger.debug(`File too large: ${file.size} bytes for file ${file.name}`);
    return null;
  }

  try {
    // Download the file from Slack
    const downloadUrl = file.url_private_download || file.url_private;
    logger.info(`Downloading Slack file: ${file.name} from ${downloadUrl.substring(0, 80)}...`);
    const buffer = await downloadSlackFile(downloadUrl, botToken);

    // Verify buffer size matches expected (helps catch auth issues early)
    if (buffer.length < 100 && file.size > 100) {
      logger.info(
        `Downloaded buffer (${buffer.length} bytes) much smaller than expected file size (${file.size} bytes) - possible auth issue`
      );
    }

    // Upload to S3 and get S3 URI (A2A server has permissions to download directly)
    const uploadedFile: UploadedFile = await fileStorageService.uploadFile(
      buffer,
      file.name,
      file.mimetype,
      userId,
      contextId
    );

    logger.info(
      `Successfully processed file to S3: ${file.name} (${file.mimetype}, ${file.size} bytes) -> ${uploadedFile.s3Uri}`
    );

    return {
      name: file.name,
      mimeType: file.mimetype,
      url: uploadedFile.s3Uri,
      size: file.size,
    };
  } catch (error) {
    logger.error(`Failed to process file ${file.name} to S3: ${error}`);
    return null;
  }
}

/**
 * Process multiple Slack files to S3
 */
export async function processSlackFilesToS3(
  files: SlackFile[],
  botToken: string,
  fileStorageService: FileStorageService,
  userId: string,
  contextId: string
): Promise<ProcessedFileWithUrl[]> {
  const results = await Promise.all(
    files.map((file) => processSlackFileToS3(file, botToken, fileStorageService, userId, contextId))
  );

  // Filter out nulls (failed or unsupported files)
  return results.filter((f): f is ProcessedFileWithUrl => f !== null);
}
