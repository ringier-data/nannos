import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { cn } from '@/lib/utils';

interface MarkdownProps {
  children: string;
  className?: string;
  /** Use inverted colors (for dark backgrounds like primary) */
  inverted?: boolean;
}

/**
 * Renders markdown content using react-markdown with consistent styling
 */
export function Markdown({ children, className, inverted = false }: MarkdownProps) {
  if (!children || typeof children !== 'string') {
    return null;
  }

  return (
    <div
      className={cn(
        'prose prose-sm max-w-none',
        inverted ? 'prose-invert' : 'dark:prose-invert',
        // Headings
        'prose-headings:font-semibold prose-headings:mt-4 prose-headings:mb-2',
        'prose-h1:text-xl prose-h2:text-lg prose-h3:text-base',
        'prose-h1:border-b prose-h1:border-border prose-h1:pb-1',
        // Tables
        'prose-table:my-2 prose-th:px-3 prose-th:py-1.5 prose-td:px-3 prose-td:py-1.5',
        inverted
          ? 'prose-th:border-white/20 prose-td:border-white/20'
          : 'prose-th:border-border prose-td:border-border',
        // Paragraphs
        'prose-p:my-1 prose-p:leading-relaxed',
        // Lists
        'prose-ul:my-1 prose-ol:my-1 prose-li:my-0',
        // Inline code
        inverted
          ? 'prose-code:bg-white/20 prose-code:text-inherit'
          : 'prose-code:bg-muted prose-code:text-foreground',
        'prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:font-mono prose-code:before:content-none prose-code:after:content-none',
        // Code blocks - always dark with light text, override any inherited colors
        '[&_pre]:!bg-zinc-900 [&_pre]:!text-zinc-100 [&_pre]:rounded-md [&_pre]:p-3 [&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:leading-relaxed',
        '[&_pre_code]:!text-zinc-100 [&_pre_code]:text-xs [&_pre_code]:font-mono [&_pre_code]:bg-transparent [&_pre_code]:p-0',
        // Horizontal rule
        'prose-hr:my-4',
        // Blockquotes
        'prose-blockquote:my-2 prose-blockquote:border-l-2',
        inverted ? 'prose-blockquote:border-white/30' : 'prose-blockquote:border-border',
        // Strong
        'prose-strong:font-semibold',
        // Links - make them stand out with color, underline, and hover effects
        inverted
          ? '[&_a]:!text-blue-300 [&_a]:!underline hover:[&_a]:!text-blue-200'
          : '[&_a]:!text-blue-600 dark:[&_a]:!text-blue-400 [&_a]:!underline hover:[&_a]:!text-blue-700 dark:hover:[&_a]:!text-blue-300',
        '[&_a]:!font-medium [&_a]:cursor-pointer [&_a]:transition-colors',
        className
      )}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
