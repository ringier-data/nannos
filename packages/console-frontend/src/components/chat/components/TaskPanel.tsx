import { useState } from 'react';
import { ChevronRight, ListTodo, PanelRightClose } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { useChat } from '../contexts';
import {
  capitalize,
  copyToClipboard,
  extractPartTexts,
  formatTaskStatusLabel,
  getTaskState,
  shortenIdentifier,
  shouldShowTaskProgress,
} from '../utils';
import type { Task, TaskHistoryEntry } from '../types';

interface TaskCardProps {
  task: Task;
}

interface CollapsibleSectionProps {
  title: string;
  defaultExpanded?: boolean;
  children: React.ReactNode;
}

function CollapsibleSection({ title, defaultExpanded = false, children }: CollapsibleSectionProps) {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);

  return (
    <Collapsible open={isExpanded} onOpenChange={setIsExpanded}>
      <CollapsibleTrigger asChild>
        <button className="w-full flex items-center gap-2 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors">
          <ChevronRight className={cn('w-3 h-3 transition-transform', isExpanded && 'rotate-90')} />
          <span>{title}</span>
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent className="pl-5 pb-2">{children}</CollapsibleContent>
    </Collapsible>
  );
}

function CopyableId({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const success = await copyToClipboard(value);
    if (success) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <button
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      onClick={handleCopy}
      title={`Click to copy: ${value}`}
    >
      <span>{label}:</span>
      <span className="font-mono">{shortenIdentifier(value)}</span>
      {copied && <span className="text-green-500">✓</span>}
    </button>
  );
}

function TaskHistory({ history }: { history: TaskHistoryEntry[] }) {
  if (!history || history.length === 0) return null;

  const entriesToRender = history.slice(-3);

  return (
    <div className="space-y-2">
      {entriesToRender.map((entry, index) => {
        const role = entry.role || entry.kind || 'entry';
        const parts = Array.isArray(entry.parts) ? entry.parts : [];
        const messageText = extractPartTexts(parts).join('\n');

        return (
          <div key={index} className="text-xs border-l-2 border-border pl-2 py-1">
            <div className="font-medium text-foreground">{capitalize(role)}</div>
            {entry.kind && <span className="text-muted-foreground">[{capitalize(entry.kind)}]</span>}
            {messageText && <div className="text-muted-foreground mt-0.5 line-clamp-2">{messageText}</div>}
            {entry.messageId && (
              <div className="mt-1">
                <CopyableId label="ID" value={entry.messageId} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function TaskCard({ task }: TaskCardProps) {
  const statusLabel = formatTaskStatusLabel(task.statusDetails || task.status);
  const normalizedStatus = getTaskState(task.status);
  const showProgress = shouldShowTaskProgress(task.status);
  const validationErrors = task.validationErrors || [];

  const getStatusClasses = () => {
    switch (normalizedStatus) {
      case 'completed':
      case 'succeeded':
        return 'bg-green-500/10 text-green-600 dark:text-green-400';
      case 'failed':
        return 'bg-red-500/10 text-red-600 dark:text-red-400';
      case 'running':
      case 'in_progress':
        return 'bg-blue-500/10 text-blue-600 dark:text-blue-400';
      case 'cancelled':
        return 'bg-yellow-500/10 text-yellow-600 dark:text-yellow-400';
      default:
        return 'bg-muted text-muted-foreground';
    }
  };

  return (
    <div className="rounded-lg border border-border bg-card p-3 space-y-2">
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium text-foreground">{task.title || 'Task'}</span>
        <Badge variant="secondary" className={cn('text-xs shrink-0', getStatusClasses())}>
          {statusLabel}
        </Badge>
      </div>

      {/* IDs */}
      {(task.taskId || task.id || task.contextId) && (
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {(task.taskId || task.id) && <CopyableId label="ID" value={String(task.taskId || task.id)} />}
          {task.contextId && <CopyableId label="Context" value={task.contextId} />}
        </div>
      )}

      {/* Progress Bar */}
      {showProgress && (
        <div className="space-y-1">
          <Progress value={task.progress} className="h-1" />
          <span className="text-xs text-muted-foreground">{task.progress}%</span>
        </div>
      )}

      {/* Collapsible Sections */}
      <div className="pt-1 space-y-0.5">
        {validationErrors.length > 0 && (
          <CollapsibleSection title={`Errors (${validationErrors.length})`}>
            <ul className="space-y-0.5 text-xs text-red-600 dark:text-red-400">
              {validationErrors.map((error, i) => (
                <li key={i}>• {error}</li>
              ))}
            </ul>
          </CollapsibleSection>
        )}

        {task.history && task.history.length > 0 && (
          <CollapsibleSection title={`History (${task.history.length})`}>
            <TaskHistory history={task.history} />
          </CollapsibleSection>
        )}

        {task.result && (
          <CollapsibleSection title="Result">
            <pre className="text-xs bg-muted rounded p-2 overflow-x-auto max-h-32 overflow-y-auto font-mono">
              {task.result}
            </pre>
          </CollapsibleSection>
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 px-4 text-center">
      <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center">
        <ListTodo className="w-6 h-6 text-muted-foreground" />
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">No active tasks</p>
        <p className="text-xs text-muted-foreground">Tasks will appear here when running</p>
      </div>
    </div>
  );
}

interface TaskPanelProps {
  isCollapsed: boolean;
  onToggle: () => void;
}

export function TaskPanel({ isCollapsed, onToggle }: TaskPanelProps) {
  const { tasks } = useChat();

  return (
    <div
      className={cn(
        'h-full flex flex-col bg-muted/30 border-l border-border transition-all duration-200 overflow-hidden',
        isCollapsed ? 'w-0' : 'w-72'
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <h2 className="text-sm font-semibold text-foreground">Tasks</h2>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={onToggle}
          data-testid="button-toggle-tasks"
          aria-label={isCollapsed ? 'Show task panel' : 'Hide task panel'}
          aria-expanded={!isCollapsed}
        >
          <PanelRightClose className={cn('w-4 h-4 transition-transform', isCollapsed && 'rotate-180')} />
        </Button>
      </div>

      {/* Task List */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="p-3 space-y-2">
          {tasks.length === 0 ? <EmptyState /> : tasks.map((task) => <TaskCard key={task.id} task={task} />)}
        </div>
      </ScrollArea>
    </div>
  );
}
