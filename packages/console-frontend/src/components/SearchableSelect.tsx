/**
 * A single-select picker whose options come from a paginated, server-searched
 * endpoint.
 *
 * A plain `<Select>` can only show what was fetched, so every picker built on
 * one ends up fetching a fixed slab of rows (`limit: 100`) and filtering it in
 * the browser — which silently cannot reach anything past that slab, however
 * hard the user types. This component pushes the term to the server instead and
 * pages through the matches.
 *
 * The caller owns the query: it passes the current `search` term back in, and
 * hands over the page it got. This keeps the component free of any particular
 * endpoint's shape.
 */
import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Loader2, Search } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';

const SEARCH_DEBOUNCE_MS = 300;

export interface SearchableSelectOption {
  value: string;
  label: string;
  /** Second line, e.g. an email or description. */
  hint?: string;
}

interface SearchableSelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SearchableSelectOption[];
  /** Raw search term; the component debounces before calling `onSearchChange`. */
  onSearchChange: (search: string) => void;
  /** Total matches on the server, for the "showing N of M" footer. */
  total?: number;
  isLoading?: boolean;
  disabled?: boolean;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyLabel?: string;
  className?: string;
  id?: string;
}

export function SearchableSelect({
  value,
  onChange,
  options,
  onSearchChange,
  total,
  isLoading,
  disabled,
  placeholder = 'Select...',
  searchPlaceholder = 'Search...',
  emptyLabel = 'No matches',
  className,
  id,
}: SearchableSelectProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Debounced here rather than through an effect: the term is handed to the
  // parent (which turns it into a request), and that is an event, not state to
  // synchronise on render.
  const pushSearch = (next: string) => {
    setSearch(next);
    clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => onSearchChange(next), SEARCH_DEBOUNCE_MS);
  };

  useEffect(() => () => clearTimeout(debounceTimer.current), []);

  // The chosen option's label is remembered, because the option itself lives on
  // a server page: type a search and the current selection drops out of
  // `options`, and the trigger would fall back to showing a raw id.
  const selected = options.find((o) => o.value === value);
  const [lastLabel, setLastLabel] = useState<string | null>(null);
  const shownLabel = selected?.label ?? (value ? lastLabel : null);

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) {
          clearTimeout(debounceTimer.current);
          setSearch('');
          onSearchChange('');
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          disabled={disabled}
          className={cn('justify-between font-normal', className)}
        >
          <span className={cn('truncate', !value && 'text-muted-foreground')}>
            {shownLabel ?? (value ? '…' : placeholder)}
          </span>
          <ChevronDown className="size-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-(--radix-popover-trigger-width) min-w-64 p-0"
        onOpenAutoFocus={(e) => {
          // Focus the search box, not the first row.
          e.preventDefault();
          (e.currentTarget as HTMLElement).querySelector('input')?.focus();
        }}
      >
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <Search className="text-muted-foreground size-4 shrink-0" />
          <Input
            value={search}
            onChange={(e) => pushSearch(e.target.value)}
            placeholder={searchPlaceholder}
            className="h-7 border-0 p-0 shadow-none focus-visible:ring-0"
          />
          {isLoading && <Loader2 className="text-muted-foreground size-4 shrink-0 animate-spin" />}
        </div>
        {/* Fixed height, not max-height: the popover must not resize under the
            pointer as the result count changes. */}
        <div className="h-64 overflow-y-auto py-1">
          {options.length === 0 ? (
            <p className="text-muted-foreground px-3 py-6 text-center text-sm">
              {isLoading ? 'Searching...' : emptyLabel}
            </p>
          ) : (
            options.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => {
                  onChange(option.value);
                  setLastLabel(option.label);
                  setOpen(false);
                }}
                className="hover:bg-accent flex w-full items-center gap-2 px-3 py-2 text-left text-sm"
              >
                <Check
                  className={cn('size-4 shrink-0', option.value === value ? 'opacity-100' : 'opacity-0')}
                />
                <span className="min-w-0">
                  <span className="block truncate">{option.label}</span>
                  {option.hint && (
                    <span className="text-muted-foreground block truncate text-xs">{option.hint}</span>
                  )}
                </span>
              </button>
            ))
          )}
        </div>
        {total !== undefined && total > options.length && (
          <p className="text-muted-foreground border-t px-3 py-2 text-xs">
            Showing {options.length} of {total} — keep typing to narrow.
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}
