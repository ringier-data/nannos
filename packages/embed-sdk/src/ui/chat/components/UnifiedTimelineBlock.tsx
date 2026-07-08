import React from 'react';
import { Clock } from 'lucide-react';
import type { TimelineEvent } from '../types';
import { WorkingBlock } from './WorkingBlock';
import { ThinkingBlock } from './ThinkingBlock';

interface UnifiedTimelineBlockProps {
  timeline: TimelineEvent[];
  complete: boolean;
}

function formatTimeAgo(date: Date): string {
  const seconds = Math.floor((new Date().getTime() - date.getTime()) / 1000);
  
 if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/**
 * UnifiedTimelineBlock displays events (todos, status, thoughts) in chronological order.
 * 
 * Events are rendered based on their type:
 * - todo_snapshot: WorkingBlock — shown only in completed timelines (during streaming they
 *   appear in the sticky LiveTodosWidget above the input, not inline).
 * - status: Simple text with timestamp
 * - thought_start/thought_end: ThinkingBlock (grouped by agent)
 */
export function UnifiedTimelineBlock({ timeline, complete }: UnifiedTimelineBlockProps) {
  if (timeline.length === 0) return null;

  // Group and render events
  const renderedEvents: React.JSX.Element[] = [];
  
  // Track thoughts for grouping (thought_start opens, thought_end closes)
  const activeThoughts = new Map<string, string>();  // agent_name -> content
  
  timeline.forEach((event, idx) => {
    switch (event.type) {
      case 'todo_snapshot':
        // Only show todos in completed (past-message) timelines.
        // During live streaming they are rendered by LiveTodosWidget above the input.
        if (!complete) break;
        renderedEvents.push(
          <div key={`todo-${idx}`} className="py-2">
            <WorkingBlock steps={event.todos} complete={complete} />
          </div>
        );
        break;
        
      case 'status':
        renderedEvents.push(
          <div key={`status-${idx}`} className="flex items-center gap-2 text-xs text-muted-foreground py-1">
            <Clock className="w-3 h-3 shrink-0" />
            <span className="flex-1">
              {event.source && (
                <span className="font-medium text-foreground/70">{event.source}{'  \u203A  '}</span>
              )}
              {event.message}
            </span>
            <span className="text-[10px] opacity-60">{formatTimeAgo(event.timestamp)}</span>
          </div>
        );
        break;
        
      case 'thought_start':
        // Mark thought as active (will be rendered when thought_end arrives)
        activeThoughts.set(event.agent_name, '');
        break;
        
      case 'thought_end':
        // Render accumulated thought
        if (activeThoughts.has(event.agent_name)) {
          renderedEvents.push(
            <div key={`thought-${idx}`} className="py-2">
              <ThinkingBlock 
                thoughts={[{ agent_name: event.agent_name, content: event.content }]} 
                complete={event.complete} 
              />
            </div>
          );
          activeThoughts.delete(event.agent_name);
        }
        break;
    }
  });

  return <>{renderedEvents}</>;
}
