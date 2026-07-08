import { Clock } from 'lucide-react';

interface StatusHistoryBlockProps {
  history: Array<{
    timestamp: Date;
    message: string;
  }>;
}

export function StatusHistoryBlock({ history }: StatusHistoryBlockProps) {
  if (history.length === 0) return null;

  const formatTimeAgo = (date: Date) => {
    const seconds = Math.floor((new Date().getTime() - date.getTime()) / 1000);
    
    if (seconds < 5) return 'just now';
    if (seconds < 60) return `${seconds}s ago`;
    
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  };

  return (
    <div className="my-2 space-y-1 border-l-2 border-border/50 pl-3">
      {history.map((item, index) => (
        <div key={index} className="flex items-start gap-2 text-xs">
          <Clock className="w-3 h-3 mt-0.5 shrink-0 text-muted-foreground/60" />
          <span className="flex-1 text-muted-foreground">{item.message}</span>
          <span className="text-[10px] text-muted-foreground/60 shrink-0">
            {formatTimeAgo(item.timestamp)}
          </span>
        </div>
      ))}
    </div>
  );
}
