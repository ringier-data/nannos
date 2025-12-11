import { useState } from 'react';
import { ChevronDown, ChevronRight, FileText } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';

interface AuditDiffViewerProps {
  changes: Record<string, unknown>;
}

export function AuditDiffViewer({ changes }: AuditDiffViewerProps) {
  const [isOpen, setIsOpen] = useState(false);

  if (!changes || Object.keys(changes).length === 0) {
    return <span className="text-muted-foreground">-</span>;
  }

  const hasBeforeAfter = 'before' in changes || 'after' in changes;
  const changeCount = hasBeforeAfter 
    ? Object.keys((changes.after as Record<string, unknown>) || {}).length
    : Object.keys(changes).length;

  const renderValue = (value: unknown): string => {
    if (value === null) return 'null';
    if (value === undefined) return 'undefined';
    if (typeof value === 'object') return JSON.stringify(value, null, 2);
    return String(value);
  };

  const renderDiff = () => {
    if (hasBeforeAfter) {
      const before = (changes.before as Record<string, unknown>) || {};
      const after = (changes.after as Record<string, unknown>) || {};
      const allKeys = new Set([...Object.keys(before), ...Object.keys(after)]);

      return (
        <div className="space-y-2">
          {Array.from(allKeys).map((key) => {
            const beforeValue = before[key];
            const afterValue = after[key];
            const isChanged = JSON.stringify(beforeValue) !== JSON.stringify(afterValue);
            const isNew = !(key in before);
            const isDeleted = !(key in after);

            return (
              <div key={key} className="border rounded-lg overflow-hidden shadow-sm">
                <div className="bg-muted/50 px-3 py-1.5 font-mono text-xs font-semibold border-b flex items-center gap-2">
                  <span>{key}</span>
                  {isNew && <Badge variant="default" className="h-4 text-[10px] px-1.5">NEW</Badge>}
                  {isDeleted && <Badge variant="destructive" className="h-4 text-[10px] px-1.5">DELETED</Badge>}
                  {isChanged && !isNew && !isDeleted && <Badge variant="secondary" className="h-4 text-[10px] px-1.5">CHANGED</Badge>}
                </div>
                <div className="divide-y">
                  {!isNew && (
                    <div className="flex items-start gap-2 px-3 py-2 bg-red-50/50 dark:bg-red-950/20">
                      <span className="text-red-600 dark:text-red-400 font-mono text-xs font-bold mt-0.5 select-none">−</span>
                      <pre className="text-xs flex-1 overflow-x-auto text-red-700 dark:text-red-300 font-mono">
                        {renderValue(beforeValue)}
                      </pre>
                    </div>
                  )}
                  {!isDeleted && (
                    <div className="flex items-start gap-2 px-3 py-2 bg-green-50/50 dark:bg-green-950/20">
                      <span className="text-green-600 dark:text-green-400 font-mono text-xs font-bold mt-0.5 select-none">+</span>
                      <pre className="text-xs flex-1 overflow-x-auto text-green-700 dark:text-green-300 font-mono">
                        {renderValue(afterValue)}
                      </pre>
                    </div>
                  )}
                  {!isChanged && !isNew && !isDeleted && (
                    <div className="flex items-start gap-2 px-3 py-2 bg-muted/30">
                      <span className="text-muted-foreground font-mono text-xs mt-0.5 select-none">=</span>
                      <pre className="text-xs flex-1 overflow-x-auto text-muted-foreground font-mono">
                        {renderValue(afterValue)}
                      </pre>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      );
    }

    // For simple changes without before/after structure
    return (
      <div className="space-y-2">
        {Object.entries(changes).map(([key, value]) => (
          <div key={key} className="border rounded-lg overflow-hidden shadow-sm">
            <div className="bg-muted/50 px-3 py-1.5 font-mono text-xs font-semibold border-b">
              {key}
            </div>
            <div className="px-3 py-2 bg-blue-50/30 dark:bg-blue-950/20">
              <pre className="text-xs overflow-x-auto text-foreground font-mono">
                {renderValue(value)}
              </pre>
            </div>
          </div>
        ))}
      </div>
    );
  };

  return (
    <Collapsible open={isOpen} onOpenChange={setIsOpen}>
      <div className="flex items-center gap-2">
        <CollapsibleTrigger asChild>
          <Button variant="ghost" size="sm" className="h-7 px-2">
            {isOpen ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            <FileText className="h-3.5 w-3.5 ml-1" />
            <span className="ml-1.5 text-xs">
              {changeCount} {changeCount === 1 ? 'field' : 'fields'}
            </span>
          </Button>
        </CollapsibleTrigger>
        {!isOpen && (
          <div className="flex gap-1 flex-wrap">
            {hasBeforeAfter ? (
              Object.keys((changes.after as Record<string, unknown>) || {}).slice(0, 3).map((key) => (
                <Badge key={key} variant="secondary" className="text-xs">
                  {key}
                </Badge>
              ))
            ) : (
              Object.keys(changes).slice(0, 3).map((key) => (
                <Badge key={key} variant="secondary" className="text-xs">
                  {key}
                </Badge>
              ))
            )}
            {changeCount > 3 && (
              <span className="text-xs text-muted-foreground">
                +{changeCount - 3} more
              </span>
            )}
          </div>
        )}
      </div>
      <CollapsibleContent className="mt-3">
        {renderDiff()}
      </CollapsibleContent>
    </Collapsible>
  );
}
