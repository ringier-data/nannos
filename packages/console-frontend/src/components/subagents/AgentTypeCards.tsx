import type { LucideIcon } from 'lucide-react';
import { CheckCircle2, Database, Globe, MessageSquare, Terminal } from 'lucide-react';
import { cn } from '@/lib/utils';

/**
 * What the admin picks on the create page. The first three are sub-agent types the
 * backend knows; `embedded` is a local sub-agent whose definition an application
 * publishes, so it is created through a different endpoint and has its own form.
 */
export type AgentTypeChoice = 'local' | 'remote' | 'foundry' | 'embedded';

interface AgentTypeCardProps {
  icon: LucideIcon;
  title: string;
  description: string;
  selected: boolean;
  disabled?: boolean;
  onClick: () => void;
}

function AgentTypeCard({ icon: Icon, title, description, selected, disabled, onClick }: AgentTypeCardProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'relative flex flex-col items-start p-6 rounded-lg border-2 transition-all text-left',
        'hover:shadow-md focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
        selected ? 'border-primary bg-primary/5 shadow-sm' : 'border-border bg-background hover:border-primary/50'
      )}
    >
      {selected && (
        <div className="absolute top-3 right-3">
          <CheckCircle2 className="h-5 w-5 text-primary" />
        </div>
      )}
      <div className="flex items-center gap-3 mb-3">
        <div className={cn('p-2 rounded-md', selected ? 'bg-primary/10' : 'bg-muted')}>
          <Icon className="h-6 w-6" />
        </div>
        <h4 className="text-base font-semibold">{title}</h4>
      </div>
      <p className="text-sm text-muted-foreground">{description}</p>
    </button>
  );
}

interface AgentTypeCardsProps {
  value: AgentTypeChoice;
  onChange: (choice: AgentTypeChoice) => void;
  disabled?: boolean;
  /** Show the embedded card. Admins in admin mode only, and never while editing. */
  showEmbedded?: boolean;
}

/** The card row at the top of the create page. Shared by the standard and embedded forms. */
export function AgentTypeCards({ value, onChange, disabled = false, showEmbedded = false }: AgentTypeCardsProps) {
  return (
    <div className={cn('grid grid-cols-1 gap-4', showEmbedded ? 'md:grid-cols-2 xl:grid-cols-4' : 'md:grid-cols-3')}>
      <AgentTypeCard
        icon={Terminal}
        title="Local Agent"
        description="Run an agent locally with a custom system prompt and optional MCP tools. Full control over behavior and capabilities."
        selected={value === 'local'}
        disabled={disabled}
        onClick={() => onChange('local')}
      />
      <AgentTypeCard
        icon={Globe}
        title="Remote Agent (A2A)"
        description="Connect to an external A2A-compatible agent endpoint. Delegate tasks to specialized external services."
        selected={value === 'remote'}
        disabled={disabled}
        onClick={() => onChange('remote')}
      />
      <AgentTypeCard
        icon={Database}
        title="Foundry Agent"
        description="Connect to Palantir Foundry ontology queries. Execute data operations and workflows on Foundry."
        selected={value === 'foundry'}
        disabled={disabled}
        onClick={() => onChange('foundry')}
      />
      {showEmbedded && (
        <AgentTypeCard
          icon={MessageSquare}
          title="Nannos Assistant"
          description="Integrate Nannos Assistant in your application. Your application exposes prompt and skills over HTTPS, Nannos uses them to run the agent."
          selected={value === 'embedded'}
          disabled={disabled}
          onClick={() => onChange('embedded')}
        />
      )}
    </div>
  );
}
