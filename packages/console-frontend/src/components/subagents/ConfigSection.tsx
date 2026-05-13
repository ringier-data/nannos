import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

interface ConfigSectionProps {
  title: string;
  icon?: LucideIcon;
  defaultOpen?: boolean;
  children: React.ReactNode;
  badge?: React.ReactNode;
}

export function ConfigSection({ title, icon: Icon, defaultOpen = true, children, badge }: ConfigSectionProps) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="border border-border/50 rounded-md overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full px-3 py-2 text-left hover:bg-accent/50 transition-colors"
      >
        {Icon && <Icon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />}
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex-1">{title}</span>
        {badge}
        <ChevronDown className={`h-3.5 w-3.5 text-muted-foreground transition-transform ${open ? '' : '-rotate-90'}`} />
      </button>
      {open && <div className="px-3 pb-3 pt-1 space-y-3 min-w-0">{children}</div>}
    </div>
  );
}
