import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

interface SubAgentOption {
  id: number;
  name: string;
  type?: string | null;
}

/**
 * Sub-agent picker shared by the scheduler forms. `includeNone` adds a
 * "None (notify only)" entry that maps to the empty string (watch jobs,
 * where the sub-agent is optional).
 */
export function SubAgentSelect({
  value,
  onChange,
  subAgents,
  disabled,
  includeNone,
}: {
  value: string;
  onChange: (v: string) => void;
  subAgents: SubAgentOption[];
  disabled?: boolean;
  includeNone?: boolean;
}) {
  const options = subAgents.filter((sa) => sa.name !== 'voice-agent');
  // The list is owned-only, but a job may reference an agent merely shared
  // with the user; it must still display and survive a save round-trip.
  const unlisted = value !== '' && !options.some((sa) => String(sa.id) === value);
  return (
    <Select
      value={includeNone ? value || '_none' : value}
      onValueChange={(v) => onChange(v === '_none' ? '' : v)}
      disabled={disabled}
    >
      <SelectTrigger>
        <SelectValue placeholder={includeNone ? 'None (notify only)' : 'Select a sub-agent…'} />
      </SelectTrigger>
      <SelectContent>
        {includeNone && (
          <SelectItem value="_none">
            <span className="text-muted-foreground">None (notify only)</span>
          </SelectItem>
        )}
        {unlisted && (
          <SelectItem value={value}>
            <span>Agent #{value}</span>
            <span className="ml-2 text-xs text-muted-foreground">(not in your list)</span>
          </SelectItem>
        )}
        {options.length === 0 && !includeNone && !unlisted ? (
          <div className="px-3 py-2 text-sm text-muted-foreground">No sub-agents found</div>
        ) : (
          options.map((sa) => (
            <SelectItem key={sa.id} value={String(sa.id)}>
              <span>{sa.name}</span>
              {sa.type === 'automated' && (
                <span className="ml-2 text-xs text-muted-foreground">(automated)</span>
              )}
            </SelectItem>
          ))
        )}
      </SelectContent>
    </Select>
  );
}
