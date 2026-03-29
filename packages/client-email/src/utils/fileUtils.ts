

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
 * Convert a buffer to base64 string
 */
export function bufferToBase64(buffer: Buffer): string {
  return buffer.toString('base64');
}

/**
 * Decode base64 data to a buffer
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
 */
export interface ProcessedFileWithUrl {
  name: string;
  mimeType: string;
  url: string; // S3 URI (s3://bucket/key)
  size: number;
}
