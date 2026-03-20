// Utility functions for the chat application

import { v7 as uuidv7 } from 'uuid';

/**
 * Generate a UUID v7
 */
export function generateUUID(): string {
  return uuidv7();
}

/**
 * Escape HTML special characters
 */
export function escapeHtml(text: string | undefined | null): string {
  if (text === undefined || text === null) return '';
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

/**
 * Truncate text with ellipsis
 */
export function truncateText(text: string | undefined | null, maxLength = 150): string {
  if (!text) return '';
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + '...';
}

/**
 * Shorten an identifier for display (e.g., UUIDs)
 */
export function shortenIdentifier(value: string | undefined | null): string {
  if (!value || typeof value !== 'string') return '';
  if (value.length <= 10) return value;
  return `${value.slice(0, 4)}...${value.slice(-4)}`;
}

/**
 * Capitalize the first letter of a string
 */
export function capitalize(text: string | undefined | null): string {
  if (text === undefined || text === null) return '';
  const str = String(text);
  if (!str.length) return '';
  return str.charAt(0).toUpperCase() + str.slice(1);
}

/**
 * Humanize status text (replace underscores/dashes with spaces, capitalize)
 */
export function humanizeStatusText(text: string | undefined | null): string {
  if (text === undefined || text === null) return '';
  const cleaned = String(text).replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (!cleaned.length) return '';
  const normalized = cleaned.toLowerCase();
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

/**
 * Get initials from a name or email
 */
export function getInitials(input: string | undefined | null): string {
  if (!input) return '?';

  const normalized = String(input).trim();
  if (!normalized) return '?';

  const sanitized = normalized.includes('@') ? normalized.split('@')[0].replace(/[._-]+/g, ' ') : normalized;

  const parts = sanitized.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';

  const first = parts[0].charAt(0) || '';
  const last = parts.length > 1 ? parts[parts.length - 1].charAt(0) : '';
  const combined = `${first}${last}`.trim().toUpperCase();
  return combined || first.toUpperCase() || '?';
}

/**
 * Format a date as a locale string
 */
export function formatTimestamp(date: Date | string | undefined | null): string {
  if (!date) return '';
  const d = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(d.getTime())) return '';
  try {
    return d.toLocaleString();
  } catch {
    return '';
  }
}

/**
 * Format a date as time only (HH:MM)
 */
export function formatTime(date: Date | string | undefined | null): string {
  if (!date) return '';
  const d = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(d.getTime())) return '';
  try {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

/**
 * Check if a task status indicates completion
 */
export function isTaskComplete(status: string | undefined | null): boolean {
  const normalized = (status || '').toLowerCase();
  return ['completed', 'failed', 'succeeded', 'cancelled'].includes(normalized);
}

/**
 * Check if a task should show progress bar
 */
export function shouldShowTaskProgress(status: string | undefined | null): boolean {
  const normalized = (status || '').toLowerCase();
  return normalized === 'running' || normalized === 'in_progress';
}

/**
 * Get normalized task state from status object or string
 */
export function getTaskState(status: string | { state?: string; message?: string } | undefined | null): string {
  if (!status) return 'unknown';
  if (typeof status === 'string') return status.toLowerCase();
  if (typeof status === 'object') {
    if (typeof status.state === 'string') return status.state.toLowerCase();
    if (typeof status.message === 'string') return status.message.toLowerCase();
  }
  return 'unknown';
}

/**
 * Format task status label for display
 */
export function formatTaskStatusLabel(status: string | { label?: string; state?: string } | undefined | null): string {
  if (!status) return 'Unknown';
  if (typeof status === 'string') return humanizeStatusText(status);
  if (typeof status === 'object') {
    if (typeof status.label === 'string') return status.label;
    if (typeof status.state === 'string') return humanizeStatusText(status.state);
  }
  return 'Unknown';
}

/**
 * Apply basic inline markdown (bold, italic, code) to text
 */
export function applyInlineMarkdown(text: string): string {
  if (!text) return '';
  let escaped = escapeHtml(text);
  // Inline code (must be before bold/italic to avoid conflicts)
  escaped = escaped.replace(/`([^`]+)`/g, '<code class="bg-muted px-1.5 py-0.5 rounded text-sm font-mono">$1</code>');
  // Bold
  escaped = escaped.replace(/(\*\*|__)(.+?)\1/g, '<strong>$2</strong>');
  // Italic (single * or _)
  escaped = escaped.replace(/(\*|_)([^*_]+?)\1/g, '<em>$2</em>');
  return escaped;
}

/**
 * Convert markdown to HTML (with code block support)
 */
export function convertMarkdownToHtml(markdown: string): string {
  if (!markdown || typeof markdown !== 'string') {
    return `<p>${escapeHtml(markdown)}</p>`;
  }

  // First, handle code blocks (``` ... ```)
  const codeBlockRegex = /```(\w*)\n?([\s\S]*?)```/g;
  const codeBlocks: string[] = [];
  let processedMarkdown = markdown.replace(codeBlockRegex, (_, lang, code) => {
    const placeholder = `__CODE_BLOCK_${codeBlocks.length}__`;
    const languageLabel = lang ? `<div class="text-xs text-muted-foreground px-3 py-1 border-b border-border">${escapeHtml(lang)}</div>` : '';
    codeBlocks.push(
      `<div class="rounded-md border border-border bg-muted/50 overflow-hidden my-2">${languageLabel}<pre class="p-3 overflow-x-auto"><code class="text-sm font-mono">${escapeHtml(code.trim())}</code></pre></div>`
    );
    return placeholder;
  });

  const lines = processedMarkdown.split('\n');
  const parts: string[] = [];
  let orderedBuffer: string[] = [];
  let unorderedBuffer: string[] = [];

  const flushOrdered = () => {
    if (orderedBuffer.length) {
      const items = orderedBuffer.map((item) => `<li>${item}</li>`).join('');
      parts.push(`<ol class="list-decimal list-inside space-y-1 my-2">${items}</ol>`);
      orderedBuffer = [];
    }
  };

  const flushUnordered = () => {
    if (unorderedBuffer.length) {
      const items = unorderedBuffer.map((item) => `<li>${item}</li>`).join('');
      parts.push(`<ul class="list-disc list-inside space-y-1 my-2">${items}</ul>`);
      unorderedBuffer = [];
    }
  };

  lines.forEach((rawLine) => {
    const line = rawLine.trim();
    
    // Check for code block placeholder
    const placeholderMatch = line.match(/^__CODE_BLOCK_(\d+)__$/);
    if (placeholderMatch) {
      flushOrdered();
      flushUnordered();
      parts.push(codeBlocks[parseInt(placeholderMatch[1], 10)]);
      return;
    }
    
    if (!line) {
      flushOrdered();
      flushUnordered();
      return;
    }

    // Horizontal rule (---, ***, ___)
    if (/^[-*_]{3,}$/.test(line)) {
      flushOrdered();
      flushUnordered();
      parts.push('<hr class="my-3 border-border" />');
      return;
    }

    const headingMatch = line.match(/^(#{1,3})\s+(.*)$/);
    if (headingMatch) {
      flushOrdered();
      flushUnordered();
      const level = headingMatch[1].length;
      const headingText = applyInlineMarkdown(headingMatch[2]);
      const headingClasses = level === 1 ? 'text-lg font-bold' : level === 2 ? 'text-base font-semibold' : 'text-sm font-medium';
      parts.push(`<h${level} class="${headingClasses}">${headingText}</h${level}>`);
      return;
    }

    const orderedMatch = line.match(/^\d+\.\s+(.*)$/);
    if (orderedMatch) {
      flushUnordered();
      orderedBuffer.push(applyInlineMarkdown(orderedMatch[1]));
      return;
    }

    const unorderedMatch = line.match(/^[-*]\s+(.*)$/);
    if (unorderedMatch) {
      flushOrdered();
      unorderedBuffer.push(applyInlineMarkdown(unorderedMatch[1]));
      return;
    }

    flushOrdered();
    flushUnordered();
    parts.push(`<p>${applyInlineMarkdown(line)}</p>`);
  });

  flushOrdered();
  flushUnordered();

  if (!parts.length) {
    return `<p>${escapeHtml(markdown)}</p>`;
  }

  return parts.join('');
}

/**
 * Extract text from message parts array
 */
export function extractPartTexts(parts: Array<{ text?: string } | string> | undefined | null): string[] {
  if (!Array.isArray(parts)) return [];
  return parts.map((part) => {
    if (part && typeof (part as { text?: string }).text === 'string') {
      return (part as { text?: string }).text!;
    }
    if (typeof part === 'string') {
      return part;
    }
    return '';
  });
}

/**
 * Check if message parts should be displayed
 */
export function shouldDisplayMessageParts(parts: Array<{ text?: string }> | undefined | null): boolean {
  return Array.isArray(parts) && parts.length > 0;
}

/**
 * Copy text to clipboard with fallback
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;

  const canUseClipboard =
    typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function';

  if (canUseClipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to fallback
    }
  }

  // Fallback for older browsers
  try {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '-1000px';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    const successful = document.execCommand('copy');
    document.body.removeChild(textarea);
    return successful;
  } catch {
    return false;
  }
}
