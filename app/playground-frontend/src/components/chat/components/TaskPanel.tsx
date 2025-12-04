import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
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
    <Collapsible open={isExpanded} onOpenChange={setIsExpanded} className="border-t border-border">
      <CollapsibleTrigger asChild>
        <Button variant="ghost" className="w-full justify-start gap-2 py-2 h-auto hover:bg-accent/50">
          <ChevronRight className={cn('w-4 h-4 transition-transform', isExpanded && 'rotate-90')} />
          <span className="font-medium">{title}</span>
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="pb-3">{children}</CollapsibleContent>
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
    <Button variant="secondary" size="sm" className="h-auto py-1 px-2 gap-1" onClick={handleCopy} title={value}>
      <span className="text-muted-foreground text-xs">{label}</span>
      <span className="font-mono text-xs">{shortenIdentifier(value)}</span>
      {copied && <span className="text-green-500 ml-1">✓</span>}
    </Button>
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
          <div key={index} className="text-xs border-l-2 border-border pl-2">
            <div className="font-medium text-foreground">{capitalize(role)}</div>
            {entry.kind && <span className="text-muted-foreground">[{capitalize(entry.kind)}]</span>}
            {messageText && <div className="text-muted-foreground mt-1 line-clamp-3">{messageText}</div>}
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

  const getStatusVariant = (): 'default' | 'secondary' | 'destructive' | 'outline' => {
    switch (normalizedStatus) {
      case 'completed':
      case 'succeeded':
        return 'default';
      case 'failed':
        return 'destructive';
      default:
        return 'secondary';
    }
  };

  const getStatusClasses = () => {
    switch (normalizedStatus) {
      case 'completed':
      case 'succeeded':
        return 'bg-green-500/20 text-green-400 border-green-500/30 hover:bg-green-500/30';
      case 'failed':
        return 'bg-red-500/20 text-red-400 border-red-500/30 hover:bg-red-500/30';
      case 'running':
      case 'in_progress':
        return 'bg-blue-500/20 text-blue-400 border-blue-500/30 hover:bg-blue-500/30';
      case 'cancelled':
        return 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30 hover:bg-yellow-500/30';
      default:
        return '';
    }
  };

  return (
    <Card>
      <CardHeader className="p-4 pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="font-medium text-foreground">{task.title || 'Task'}</div>
          <Badge variant={getStatusVariant()} className={getStatusClasses()}>
            {statusLabel}
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="p-4 pt-0 space-y-3">
        {/* Metadata */}
        {(task.taskId || task.id || task.contextId) && (
          <div className="flex flex-wrap gap-2">
            {(task.taskId || task.id) && <CopyableId label="ID" value={String(task.taskId || task.id)} />}
            {task.contextId && <CopyableId label="Context" value={task.contextId} />}
          </div>
        )}

        {/* Progress Bar */}
        {showProgress && <Progress value={task.progress} className="h-1.5" />}

        {/* Collapsible Sections */}
        <div>
          {/* Validation Errors */}
          <CollapsibleSection title={validationErrors.length > 0 ? `Errors: ${validationErrors.length}` : 'No errors'}>
            {validationErrors.length > 0 ? (
              <ul className="space-y-1 text-xs text-red-400">
                {validationErrors.map((error, i) => (
                  <li key={i}>{error}</li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">No validation errors</p>
            )}
          </CollapsibleSection>

          {/* History */}
          {task.history && task.history.length > 0 && (
            <CollapsibleSection title="Task History">
              <TaskHistory history={task.history} />
            </CollapsibleSection>
          )}

          {/* Artifacts */}
          {task.result && (
            <CollapsibleSection title="Artifacts">
              <pre className="text-xs bg-secondary/50 rounded p-2 overflow-x-auto max-h-40 overflow-y-auto">
                {task.result}
              </pre>
            </CollapsibleSection>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function EmptyState() {
  return <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">No active tasks</div>;
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
        'flex flex-col bg-sidebar border-l border-border transition-all duration-200',
        isCollapsed ? 'w-0 overflow-hidden' : 'w-100 min-w-50 max-w-125'
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-border">
        <h2 className="text-lg font-semibold">Active Tasks</h2>
        <Button
          variant="ghost"
          size="icon"
          onClick={onToggle}
          data-testid="button-toggle-tasks"
          aria-label={isCollapsed ? 'Show task panel' : 'Hide task panel'}
          aria-expanded={!isCollapsed}
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 20 20"
            fill="currentColor"
            className={cn('transition-transform', isCollapsed && 'rotate-180')}
          >
            <path d="M6.83496 3.99992C6.38353 4.00411 6.01421 4.0122 5.69824 4.03801C5.31232 4.06954 5.03904 4.12266 4.82227 4.20012L4.62207 4.28606C4.18264 4.50996 3.81498 4.85035 3.55859 5.26848L3.45605 5.45207C3.33013 5.69922 3.25006 6.01354 3.20801 6.52824C3.16533 7.05065 3.16504 7.71885 3.16504 8.66301V11.3271C3.16504 12.2712 3.16533 12.9394 3.20801 13.4618C3.25006 13.9766 3.33013 14.2909 3.45605 14.538L3.55859 14.7216C3.81498 15.1397 4.18266 15.4801 4.62207 15.704L4.82227 15.79C5.03904 15.8674 5.31234 15.9205 5.69824 15.9521C6.01398 15.9779 6.383 15.986 6.83398 15.9902L6.83496 3.99992ZM18.165 11.3271C18.165 12.2493 18.1653 12.9811 18.1172 13.5702C18.0745 14.0924 17.9916 14.5472 17.8125 14.9648L17.7295 15.1415C17.394 15.8 16.8834 16.3511 16.2568 16.7353L15.9814 16.8896C15.5157 17.1268 15.0069 17.2285 14.4102 17.2773C13.821 17.3254 13.0893 17.3251 12.167 17.3251H7.83301C6.91071 17.3251 6.17898 17.3254 5.58984 17.2773C5.06757 17.2346 4.61294 17.1508 4.19531 16.9716L4.01855 16.8896C3.36014 16.5541 2.80898 16.0434 2.4248 15.4169L2.27051 15.1415C2.03328 14.6758 1.93158 14.167 1.88281 13.5702C1.83468 12.9811 1.83496 12.2493 1.83496 11.3271V8.66301C1.83496 7.74072 1.83468 7.00898 1.88281 6.41985C1.93157 5.82309 2.03329 5.31432 2.27051 4.84856L2.4248 4.57317C2.80898 3.94666 3.36012 3.436 4.01855 3.10051L4.19531 3.0175C4.61285 2.83843 5.06771 2.75548 5.58984 2.71281C6.17898 2.66468 6.91071 2.66496 7.83301 2.66496H12.167C13.0893 2.66496 13.821 2.66468 14.4102 2.71281C15.0069 2.76157 15.5157 2.86329 15.9814 3.10051L16.2568 3.25481C16.8833 3.63898 17.394 4.19012 17.7295 4.84856L17.8125 5.02531C17.9916 5.44285 18.0745 5.89771 18.1172 6.41985C18.1653 7.00898 18.165 7.74072 18.165 8.66301V11.3271ZM8.16406 15.995H12.167C13.1112 15.995 13.7794 15.9947 14.3018 15.9521C14.8164 15.91 15.1308 15.8299 15.3779 15.704L15.5615 15.6015C15.9797 15.3451 16.32 14.9774 16.5439 14.538L16.6299 14.3378C16.7074 14.121 16.7605 13.8478 16.792 13.4618C16.8347 12.9394 16.835 12.2712 16.835 11.3271V8.66301C16.835 7.71885 16.8347 7.05065 16.792 6.52824C16.7605 6.14232 16.7073 5.86904 16.6299 5.65227L16.5439 5.45207C16.32 5.01264 15.9796 4.64498 15.5615 4.3886L15.3779 4.28606C15.1308 4.16013 14.8165 4.08006 14.3018 4.03801C13.7794 3.99533 13.1112 3.99504 12.167 3.99504H8.16406C8.16407 3.99667 8.16504 3.99829 8.16504 3.99992L8.16406 15.995Z" />
          </svg>
        </Button>
      </div>

      {/* Task List */}
      <ScrollArea className="flex-1">
        <div className="p-4 space-y-4">
          {tasks.length === 0 ? <EmptyState /> : tasks.map((task) => <TaskCard key={task.id} task={task} />)}
        </div>
      </ScrollArea>
    </div>
  );
}
