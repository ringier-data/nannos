import { useEffect, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';

interface ExpandableTextProps {
  text: string;
  className?: string;
}

/**
 * Two-line-clamped text with a "Show more"/"Show less" toggle that only
 * renders when the text is actually truncated. Uses wrap-anywhere so long
 * unbroken tokens don't inflate the intrinsic width of ancestors (Radix
 * ScrollArea wraps content in a display:table div that grows to min-content).
 */
export function ExpandableText({ text, className }: ExpandableTextProps) {
  const [expanded, setExpanded] = useState(false);
  const [isClamped, setIsClamped] = useState(false);
  const textRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    const el = textRef.current;
    if (!el || expanded) return;
    const check = () => setIsClamped(el.scrollHeight > el.clientHeight + 1);
    check();
    const observer = new ResizeObserver(check);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, expanded]);

  return (
    <>
      <p ref={textRef} className={cn('wrap-anywhere', !expanded && 'line-clamp-2', className)}>
        {text}
      </p>
      {(isClamped || expanded) && (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          className="mb-1.5 flex items-center gap-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
        >
          {expanded ? 'Show less' : 'Show more'}
          <ChevronDown className={cn('h-3 w-3 transition-transform', expanded && 'rotate-180')} />
        </button>
      )}
    </>
  );
}
