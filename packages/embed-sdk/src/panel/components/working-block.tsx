/**
 * The live work plan: a compact, collapsible checklist of `TodoItem`s.
 * Orchestrator steps (no `source`) render first, then one group per sub-agent
 * source in first-seen order. Built on the vendored ai-elements `task.tsx`
 * (Collapsible trigger + indented content).
 */
import type { ReactNode } from 'react';
import {
  CheckCircle2Icon,
  CircleIcon,
  Loader2Icon,
  XCircleIcon,
} from 'lucide-react';
import { Task, TaskContent, TaskItem, TaskTrigger } from '../../components/ai-elements/task';
import { cn } from '../../lib/utils';
import { useStrings } from '../../react';
import type { TodoItem } from '../../transport';

export interface WorkingBlockProps {
  todos: TodoItem[];
  className?: string;
}

const STATE_ICON: Record<TodoItem['state'], ReactNode> = {
  submitted: <CircleIcon className="size-3.5 shrink-0 text-muted-foreground" />,
  working: <Loader2Icon className="size-3.5 shrink-0 animate-spin text-primary" />,
  completed: <CheckCircle2Icon className="size-3.5 shrink-0 text-emerald-600" />,
  failed: <XCircleIcon className="size-3.5 shrink-0 text-destructive" />,
};

interface TodoGroup {
  source: string;
  items: TodoItem[];
}

function groupTodos(todos: TodoItem[]): TodoGroup[] {
  const orchestrator: TodoGroup = { source: '', items: [] };
  const bySource = new Map<string, TodoGroup>();
  for (const todo of todos) {
    const source = todo.source ?? '';
    if (!source) {
      orchestrator.items.push(todo);
      continue;
    }
    let group = bySource.get(source);
    if (!group) {
      group = { source, items: [] };
      bySource.set(source, group);
    }
    group.items.push(todo);
  }
  const groups = [...bySource.values()];
  return orchestrator.items.length > 0 ? [orchestrator, ...groups] : groups;
}

export function WorkingBlock({ todos, className }: WorkingBlockProps) {
  const strings = useStrings();
  if (todos.length === 0) return null;
  const groups = groupTodos(todos);

  return (
    <Task
      data-slot="nannos-working-block"
      className={cn('rounded-md border bg-muted/30 px-3 py-2 text-sm', className)}
      defaultOpen
    >
      <TaskTrigger data-slot="nannos-working-toggle" title={strings['working.title']} />
      <TaskContent>
        {groups.map((group) => (
          <div key={group.source || 'orchestrator'} className="space-y-1">
            {group.source && (
              <div className="font-medium text-muted-foreground text-xs">{group.source}</div>
            )}
            {group.items.map((todo, index) => (
              <TaskItem key={`${group.source}:${index}:${todo.name}`} className="flex items-start gap-2">
                <span className="mt-0.5">{STATE_ICON[todo.state]}</span>
                <span
                  className={cn(
                    'min-w-0 break-words',
                    todo.state === 'completed' && 'line-through opacity-70',
                  )}
                >
                  {todo.name}
                </span>
              </TaskItem>
            ))}
          </div>
        ))}
      </TaskContent>
    </Task>
  );
}
