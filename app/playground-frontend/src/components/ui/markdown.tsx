import ReactMarkdown from 'react-markdown';
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
        'prose-headings:font-semibold prose-headings:mt-2 prose-headings:mb-1',
        'prose-h1:text-lg prose-h2:text-base prose-h3:text-sm',
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
        'prose-hr:my-2',
        // Links
        inverted
          ? 'prose-a:text-inherit prose-a:underline'
          : 'prose-a:text-primary prose-a:underline',
        className
      )}
    >
      <ReactMarkdown>{children}</ReactMarkdown>
    </div>
  );
}
