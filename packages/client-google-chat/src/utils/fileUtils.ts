import { Logger } from './logger.js';
import { FileStorageService, UploadedFile } from '../services/fileStorageService.js';
import { GoogleChatService } from '../services/googleChatService.js';

const logger = Logger.getLogger('fileUtils');

/**
 * Google Chat attachment from event payload
 */
export interface GoogleChatAttachment {
  name: string; // resource name: spaces/xxx/messages/yyy/attachments/zzz
  contentName: string; // filename
  contentType: string; // MIME type
  attachmentDataRef?: {
    resourceName: string;
  };
  source: 'DRIVE_FILE' | 'UPLOADED_CONTENT';
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
  'audio/x-m4a', // m4a (alternative)
  'audio/wav',
  'audio/wave',
  'audio/x-wav',
  'audio/webm',
  'audio/ogg',
  'audio/aac',
  'audio/flac',
]);

/**
 * Maximum file size to process (10MB)
 */
const MAX_FILE_SIZE = 10 * 1024 * 1024;

/**
 * Check if a file type is supported for processing
 */
function isSupportedFileType(mimeType: string): boolean {
  return SUPPORTED_MIME_TYPES.has(mimeType);
}

/**
 * Check if a file is within size limits
 */
function isFileSizeAllowed(size: number): boolean {
  return size <= MAX_FILE_SIZE;
}

/**
 * Download a file attachment from Google Chat using the Chat service
 */
async function downloadGoogleChatAttachment(
  projectId: string,
  userEmail: string,
  attachment: GoogleChatAttachment,
  chatService: GoogleChatService
): Promise<{ buffer: Buffer; contentType: string; fileName: string } | null> {
  logger.debug(`Downloading attachment: ${attachment.name}`);

  try {
    const attachmentMetadata = attachment.attachmentDataRef?.resourceName
      ? {
          resourceName: attachment.attachmentDataRef.resourceName,
          contentType: attachment.contentType,
          fileName: attachment.contentName,
        }
      : undefined;
    
    if (!attachmentMetadata) {
      logger.error(`No attachmentDataRef available for attachment ${attachment.name}`);
      return null;
    }

    const result = await chatService.downloadAttachment(projectId, userEmail, attachmentMetadata);
    if (!result) {
      logger.error(`Failed to download attachment ${attachment.name}`);
      return null;
    }

    logger.debug(`Downloaded ${result.data.length} bytes, Content-Type: ${result.contentType}`);

    return {
      buffer: result.data,
      contentType: result.contentType,
      fileName: result.fileName,
    };
  } catch (error) {
    logger.error(`Failed to download attachment ${attachment.name}: ${error}`);
    return null;
  }
}

/**
 * Get a human-readable description of file processing issues
 */
export function getFileProcessingWarnings(attachments: GoogleChatAttachment[]): string[] {
  const warnings: string[] = [];

  for (const attachment of attachments) {
    if (!isSupportedFileType(attachment.contentType)) {
      warnings.push(`- ${attachment.contentName}: Unsupported file type (${attachment.contentType})`);
    }
  }

  return warnings;
}

/**
 * Decode base64 data to a buffer (for posting file artifacts)
 */
export function base64ToBuffer(base64: string): Buffer {
  return Buffer.from(base64, 'base64');
}

/**
 * Get file extension from MIME type
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
 */
interface ProcessedFileWithUrl {
  name: string;
  mimeType: string;
  url: string; // S3 URI (s3://bucket/key)
  size: number;
}

/**
 * Process a Google Chat attachment: download, upload to S3, and return S3 URI
 */
async function processAttachmentToS3(
  projectId: string,
  attachment: GoogleChatAttachment,
  chatService: GoogleChatService,
  fileStorageService: FileStorageService,
  userId: string,
  userEmail: string,
  contextId: string
): Promise<ProcessedFileWithUrl | null> {
  if (!isSupportedFileType(attachment.contentType)) {
    logger.debug(`Unsupported file type: ${attachment.contentType} for file ${attachment.contentName}`);
    return null;
  }

  try {
    const result = await downloadGoogleChatAttachment(projectId, userEmail, attachment, chatService);
    if (!result) return null;

    if (!isFileSizeAllowed(result.buffer.length)) {
      logger.debug(`File too large: ${result.buffer.length} bytes for file ${attachment.contentName}`);
      return null;
    }

    // Upload to S3
    const uploadedFile: UploadedFile = await fileStorageService.uploadFile(
      result.buffer,
      attachment.contentName,
      attachment.contentType,
      userId,
      contextId
    );

    logger.info(
      `Processed attachment to S3: ${attachment.contentName} (${attachment.contentType}, ${result.buffer.length} bytes) -> ${uploadedFile.s3Uri}`
    );

    return {
      name: attachment.contentName,
      mimeType: attachment.contentType,
      url: uploadedFile.s3Uri,
      size: result.buffer.length,
    };
  } catch (error) {
    logger.error(`Failed to process attachment ${attachment.contentName} to S3: ${error}`);
    return null;
  }
}

/**
 * Process multiple Google Chat attachments to S3
 */
export async function processAttachmentsToS3(
  projectId: string,
  attachments: GoogleChatAttachment[],
  chatService: GoogleChatService,
  fileStorageService: FileStorageService,
  userId: string,
  userEmail: string,
  contextId: string
): Promise<ProcessedFileWithUrl[]> {
  const results = await Promise.all(
    attachments.map((a) => processAttachmentToS3(projectId, a, chatService, fileStorageService, userId, userEmail, contextId))
  );
  return results.filter((f): f is ProcessedFileWithUrl => f !== null);
}
