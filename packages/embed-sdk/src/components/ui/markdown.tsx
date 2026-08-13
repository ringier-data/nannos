import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { cn } from '@/lib/utils';

interface MarkdownProps {
  children: string;
  className?: string;
  /** Use inverted colors (for dark backgrounds like primary) */
  inverted?: boolean;
}

/** Minimal shape of the hast nodes react-markdown passes to component overrides. */
interface HastNode {
  type?: string;
  tagName?: string;
  value?: string;
  children?: HastNode[];
}

const elementsOf = (node?: HastNode): HastNode[] =>
  (node?.children ?? []).filter((child) => child.type === 'element');

const textOf = (node?: HastNode): string => {
  if (!node) return '';
  if (node.type === 'text') return node.value ?? '';
  return (node.children ?? []).map(textOf).join('');
};

/** Column count taken from the first row that actually has cells. */
function columnCount(node?: HastNode): number {
  for (const section of elementsOf(node)) {
    for (const row of elementsOf(section)) {
      const cells = elementsOf(row);
      if (cells.length > 0) return cells.length;
    }
  }
  return 0;
}

/** GFM has no headerless table syntax, so agents emit `| | |` when they mean a
 *  plain key/value block. Render nothing rather than a strip of blank cells. */
function isBlankHeader(node?: HastNode): boolean {
  const rows = elementsOf(node);
  if (rows.length === 0) return true;
  return rows.every((row) => elementsOf(row).every((cell) => textOf(cell).trim() === ''));
}

/** Cells worth right-aligning in monospace: 42, CHF 195,000, 96%, 4.2 M, -3, (12). */
const NUMERIC_CELL = /^[\s(]*(?:[A-Z]{3}\s*)?[+-]?\d[\d\s.,'’]*(?:\s*[%KMB])?\s*\)?[\s)]*$/;

/** Above this length a cell is prose and may wrap; below it, it stays on one line. */
const LONG_CELL_CHARS = 48;

/**
 * Renders markdown content using react-markdown with consistent styling.
 *
 * Tables get explicit renderers rather than leaning on the typography plugin:
 * agent output regularly contains tables far wider than the 400px embedded panel,
 * and the prose defaults squeeze every column until cells wrap mid-phrase. Here a
 * wide table scrolls horizontally instead, and the common two-column case renders
 * as a right-aligned key/value block.
 */
export function Markdown({ children, className, inverted = false }: MarkdownProps) {
  if (!children || typeof children !== 'string') {
    return null;
  }

  const borderClass = inverted ? 'border-white/25' : 'border-border';

  const components: Components = {
    a: ({ href, children: linkChildren, ...props }) => (
      <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
        {linkChildren}
      </a>
    ),
    table: ({ node, children: tableChildren }) => {
      const columns = columnCount(node as HastNode);
      const isKeyValue = columns === 2;
      return (
        <div className={cn('my-2 overflow-x-auto rounded-nannos-control border', borderClass)}>
          <table
            className={cn(
              'border-collapse text-left text-xs',
              // 3+ columns: size to content and scroll in the wrapper above. Fitting
              // them into a ~370px panel is what produced the mid-phrase wrapping
              // this renderer exists to avoid.
              isKeyValue ? 'w-full' : 'w-max min-w-full',
              // 2 columns: the design's metrics block — label left, value right.
              isKeyValue &&
                '[&_tbody_td:first-child]:w-[45%] [&_tbody_td:last-child]:text-right [&_tbody_td:last-child]:whitespace-nowrap [&_tbody_td:last-child]:font-nannos-mono',
            )}
          >
            {tableChildren}
          </table>
        </div>
      );
    },
    thead: ({ node, children: headChildren }) =>
      isBlankHeader(node as HastNode) ? null : (
        <thead className={cn('border-b', borderClass, inverted ? 'bg-white/10' : 'bg-nannos-stripe')}>
          {headChildren}
        </thead>
      ),
    tr: ({ children: rowChildren }) => (
      <tr
        className={cn(
          '[&:not(:first-child)]:border-t',
          borderClass,
          inverted ? 'even:bg-white/5' : 'even:bg-nannos-stripe',
        )}
      >
        {rowChildren}
      </tr>
    ),
    th: ({ children: cellChildren, style }) => (
      <th
        style={style}
        className="px-2.5 py-1.5 text-left align-bottom text-[11px] font-semibold whitespace-nowrap uppercase tracking-wide opacity-70"
      >
        {cellChildren}
      </th>
    ),
    td: ({ node, children: cellChildren, style }) => {
      const text = textOf(node as HastNode).trim();
      const isNumeric = NUMERIC_CELL.test(text);
      // Short values ride on one line — a date range or "2 attached, 1 rejected"
      // broken across four lines is what made these tables unreadable. Only real
      // prose wraps, and then at a comfortable measure rather than a column width.
      const isShort = text.length <= LONG_CELL_CHARS;
      return (
        <td
          style={style}
          className={cn(
            'px-2.5 py-1.5 align-top leading-relaxed',
            isShort ? 'whitespace-nowrap' : 'max-w-[20rem] break-words',
            isNumeric && 'text-right font-nannos-mono',
          )}
        >
          {cellChildren}
        </td>
      );
    },
  };

  return (
    <div
      className={cn(
        'prose prose-sm max-w-none min-w-0',
        inverted ? 'prose-invert' : 'dark:prose-invert',
        // Headings
        'prose-headings:font-semibold prose-headings:mt-4 prose-headings:mb-2',
        'prose-h1:text-xl prose-h2:text-lg prose-h3:text-base',
        'prose-h1:border-b prose-h1:border-border prose-h1:pb-1',
        // Tables are styled by the component overrides above, so the prose table
        // rules are dropped here — they only fought the explicit classes.
        'prose-table:my-0',
        // Paragraphs
        'prose-p:my-1 prose-p:leading-relaxed',
        // Lists
        'prose-ul:my-1 prose-ol:my-1 prose-li:my-0',
        // Inline code
        inverted
          ? 'prose-code:bg-white/20 prose-code:text-inherit'
          : 'prose-code:bg-muted prose-code:text-foreground',
        'prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:font-nannos-mono prose-code:before:content-none prose-code:after:content-none',
        // Code blocks - always dark with light text, override any inherited colors
        '[&_pre]:!bg-zinc-900 [&_pre]:!text-zinc-100 [&_pre]:rounded-md [&_pre]:p-3 [&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:leading-relaxed',
        '[&_pre_code]:!text-zinc-100 [&_pre_code]:text-xs [&_pre_code]:font-nannos-mono [&_pre_code]:bg-transparent [&_pre_code]:p-0',
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
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
