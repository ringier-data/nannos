/**
 * Searchable MCP tool picker.
 *
 * The gateway exposes several hundred tools, which a plain `<Select>` cannot make
 * navigable: no filtering, no type-ahead, and every row repeating its server prefix.
 * This trades it for a combobox that
 *
 *  - filters locally on every word of the query, and appends the backend's semantic
 *    search (`/mcp/tools/search`) so "campaign sync" also finds tools that match by
 *    meaning rather than spelling;
 *  - groups by server and strips the server prefix from the row label, since that
 *    prefix is ~40% of the horizontal space and carries no distinguishing signal;
 *  - keeps recently used tools on top, which covers most repeat authoring.
 *
 * Supports single select (`value`/`onChange`) and multi select (`values`/`onToggle`).
 */
import { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Check, ChevronDown, Loader2, Search, X } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { consoleGrepMcpToolsOptions } from '@/api/generated/@tanstack/react-query.gen';
import type { McpTool } from '@/api/generated/types.gen';
import { toolServer, toolShortName } from '@/lib/mcpTools';
import { cn } from '@/lib/utils';

/** localStorage key holding the most recently picked tool names, newest first. */
const RECENTS_KEY = 'nannos.mcpToolSelect.recents';
const RECENTS_MAX = 5;

/** Rows rendered past this are cut off, with a hint to narrow the search. */
const MAX_ROWS = 120;

function readRecents(): string[] {
  try {
    const raw = localStorage.getItem(RECENTS_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : [];
  } catch {
    // Private windows and blocked site data both throw here; recents are a
    // convenience, so losing them must not break the picker.
    return [];
  }
}

function pushRecent(name: string): void {
  try {
    const next = [name, ...readRecents().filter((n) => n !== name)].slice(0, RECENTS_MAX);
    localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    /* ignore */
  }
}

type Row =
  | { kind: 'header'; key: string; label: string }
  | { kind: 'tool'; key: string; tool: McpTool }
  | { kind: 'note'; key: string; label: string };

interface BaseProps {
  tools: McpTool[];
  disabled?: boolean;
  /** Placeholder shown on the trigger while nothing is selected. */
  placeholder?: string;
  /** Marks the trigger invalid (per-field validation). */
  invalid?: boolean;
  id?: string;
}

interface SingleProps extends BaseProps {
  value: string;
  onChange: (toolName: string) => void;
  values?: never;
  onToggle?: never;
}

interface MultiProps extends BaseProps {
  values: string[];
  onToggle: (toolName: string) => void;
  value?: never;
  onChange?: never;
}

export function McpToolSelect(props: SingleProps | MultiProps) {
  const { tools, disabled, placeholder, invalid, id } = props;
  const multiple = props.values !== undefined;
  const selected = useMemo(
    () => new Set(multiple ? props.values : props.value ? [props.value] : []),
    [multiple, props.values, props.value],
  );

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  /** Keyboard cursor, held as a tool name so a changing result set cannot strand it. */
  const [cursorTool, setCursorTool] = useState<string | null>(null);
  const [recents, setRecents] = useState<string[]>(() => readRecents());
  const listRef = useRef<HTMLDivElement>(null);

  const trimmed = query.trim();

  // Semantic search runs against the backend, which scores name, description and
  // schema fields. Only worth a round trip once the query says something.
  const { data: semanticData, isFetching: semanticFetching } = useQuery({
    ...consoleGrepMcpToolsOptions({ query: { query: trimmed, top_k: 8 } }),
    enabled: open && trimmed.length >= 3,
    staleTime: 60_000,
  });

  const byName = useMemo(() => new Map(tools.map((t) => [t.name, t])), [tools]);

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = [];
    const pushTools = (label: string, list: McpTool[]) => {
      if (list.length === 0) return;
      out.push({ kind: 'header', key: `h:${label}`, label });
      list.forEach((t) => out.push({ kind: 'tool', key: `t:${label}:${t.name}`, tool: t }));
    };

    if (!trimmed) {
      pushTools(
        'Recently used',
        recents.map((n) => byName.get(n)).filter((t): t is McpTool => Boolean(t)),
      );
      const servers = new Map<string, McpTool[]>();
      tools.forEach((t) => {
        const server = toolServer(t);
        const bucket = servers.get(server);
        if (bucket) bucket.push(t);
        else servers.set(server, [t]);
      });
      [...servers.keys()].sort().forEach((server) => pushTools(server, servers.get(server)!));
      return out;
    }

    // Match every word separately rather than the query as one substring: people type
    // "campaign sync status", which no single tool name contains verbatim, and the
    // semantic search takes a second or two — the local list has to say something
    // useful in the meantime.
    const words = trimmed.toLowerCase().split(/\s+/).filter(Boolean);
    const nameHits = tools.filter((t) => {
      const name = t.name.toLowerCase();
      return words.every((w) => name.includes(w));
    });
    const nameHitNames = new Set(nameHits.map((t) => t.name));
    const textHits = tools.filter((t) => {
      if (nameHitNames.has(t.name)) return false;
      const haystack = `${t.name} ${t.description ?? ''}`.toLowerCase();
      return words.every((w) => haystack.includes(w));
    });
    const localNames = new Set([...nameHitNames, ...textHits.map((t) => t.name)]);
    // Prefer the local copy of a semantic hit: it carries the input_schema this
    // picker's callers need, which the search response may summarise away.
    const semantic = (semanticData?.tools ?? [])
      .filter((t) => !localNames.has(t.name))
      .map((t) => byName.get(t.name) ?? t);

    // Local hits first, semantic suggestions appended after. The semantic response
    // lands a second or two later, and inserting it above would shift the row under
    // the keyboard cursor — pressing Enter would then select a tool the user never
    // looked at, which is exactly what happened before this ordering.
    pushTools('Name matches', nameHits);
    pushTools('Description matches', textHits);
    pushTools(
      nameHits.length + textHits.length > 0 ? 'Also related' : 'Best matches',
      semantic,
    );

    if (out.length === 0) {
      out.push({
        kind: 'note',
        key: 'empty',
        label: semanticFetching
          ? 'Searching…'
          : 'No tool matches. Try describing what you need instead — the search reads descriptions too.',
      });
    }
    return out;
  }, [trimmed, tools, recents, byName, semanticData, semanticFetching]);

  const visibleRows = rows.length > MAX_ROWS ? rows.slice(0, MAX_ROWS) : rows;
  const truncated = rows.length > MAX_ROWS;
  const toolRowIndexes = useMemo(
    () => visibleRows.flatMap((r, i) => (r.kind === 'tool' ? [i] : [])),
    [visibleRows],
  );
  const toolCount = toolRowIndexes.length;

  // Derived, not stored: when the cursor's tool leaves the result set the highlight
  // falls back to the first row instead of pointing at nothing.
  const cursor = useMemo(() => {
    const at = visibleRows.findIndex((r) => r.kind === 'tool' && r.tool.name === cursorTool);
    return at === -1 ? (toolRowIndexes[0] ?? -1) : at;
  }, [visibleRows, cursorTool, toolRowIndexes]);

  function pick(tool: McpTool) {
    pushRecent(tool.name);
    setRecents(readRecents());
    if (multiple) {
      props.onToggle(tool.name);
      setQuery('');
    } else {
      props.onChange(tool.name);
      setOpen(false);
    }
  }

  function moveCursor(delta: number) {
    if (toolRowIndexes.length === 0) return;
    const at = toolRowIndexes.indexOf(cursor);
    const next = at === -1 ? 0 : (at + delta + toolRowIndexes.length) % toolRowIndexes.length;
    const index = toolRowIndexes[next];
    const row = visibleRows[index];
    if (row?.kind === 'tool') setCursorTool(row.tool.name);
    listRef.current
      ?.querySelector(`[data-row-index="${index}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveCursor(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveCursor(-1);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const row = visibleRows[cursor];
      if (row?.kind === 'tool') pick(row.tool);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  }

  const selectedTool = !multiple && props.value ? byName.get(props.value) : undefined;

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        // Reset here rather than in an effect: closing is the event that should
        // clear the search, and an effect would re-render to say the same thing.
        if (!next) {
          setQuery('');
          setCursorTool(null);
        }
      }}
    >
      <PopoverTrigger asChild>
        <button
          id={id}
          type="button"
          disabled={disabled}
          aria-invalid={invalid || undefined}
          className={cn(
            'border-input flex h-9 w-full items-center justify-between gap-2 rounded-md border bg-transparent px-3 py-2 text-sm shadow-xs transition-[color,box-shadow] outline-none',
            'focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]',
            'aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40',
            'dark:bg-input/30 dark:hover:bg-input/50 disabled:cursor-not-allowed disabled:opacity-50',
          )}
        >
          {selectedTool ? (
            <span className="flex min-w-0 items-center gap-2">
              <Badge variant="outline" className="shrink-0 text-[10px]">
                {toolServer(selectedTool)}
              </Badge>
              <span className="truncate">{toolShortName(selectedTool)}</span>
            </span>
          ) : !multiple && props.value ? (
            // A job may reference a tool the gateway no longer lists; show it rather
            // than silently rendering an empty trigger.
            <span className="truncate">{props.value}</span>
          ) : (
            <span className="text-muted-foreground">
              {placeholder ?? `Search ${tools.length || ''} tools…`.replace('  ', ' ')}
            </span>
          )}
          <ChevronDown className="size-4 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        // Tailwind v4 spells a CSS-variable value w-(--var); the v3 bracket form
        // generates nothing, and tailwind-merge drops the base w-72 either way —
        // leaving the panel to size itself to its longest description line.
        className="w-(--radix-popover-trigger-width) min-w-72 p-0"
        onOpenAutoFocus={(e) => {
          // Focus the search box, not the first row.
          e.preventDefault();
          (e.currentTarget as HTMLElement).querySelector('input')?.focus();
        }}
      >
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <Search className="text-muted-foreground size-4 shrink-0" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Search by name, or describe what you need…"
            className="h-6 border-0 px-0 shadow-none focus-visible:border-0 focus-visible:ring-0"
          />
          {semanticFetching && <Loader2 className="text-muted-foreground size-3.5 animate-spin" />}
          {query && (
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="size-5"
              onClick={() => setQuery('')}
            >
              <X className="size-3.5" />
            </Button>
          )}
        </div>

        <div ref={listRef} className="max-h-72 overflow-y-auto p-1">
          {visibleRows.map((row, index) =>
            row.kind === 'header' ? (
              <div
                key={row.key}
                className="text-muted-foreground px-2 pt-2 pb-1 text-[11px] font-semibold tracking-wide uppercase"
              >
                {row.label}
              </div>
            ) : row.kind === 'note' ? (
              <p key={row.key} className="text-muted-foreground px-2 py-3 text-xs">
                {row.label}
              </p>
            ) : (
              <button
                key={row.key}
                type="button"
                data-row-index={index}
                onClick={() => pick(row.tool)}
                onMouseEnter={() => setCursorTool(row.tool.name)}
                className={cn(
                  'flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left',
                  index === cursor && 'bg-accent',
                )}
              >
                <span className="grid min-w-0 flex-1 gap-0.5">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate text-sm font-medium">{toolShortName(row.tool)}</span>
                    <Badge variant="outline" className="shrink-0 px-1.5 text-[10px]">
                      {toolServer(row.tool)}
                    </Badge>
                  </span>
                  {row.tool.description && (
                    <span className="text-muted-foreground line-clamp-1 text-xs">
                      {row.tool.description}
                    </span>
                  )}
                </span>
                {selected.has(row.tool.name) && <Check className="mt-0.5 size-4 shrink-0" />}
              </button>
            ),
          )}
          {truncated && (
            <p className="text-muted-foreground px-2 py-2 text-xs">
              Showing the first {MAX_ROWS} of {rows.length} rows — narrow your search to see the
              rest.
            </p>
          )}
        </div>

        <div className="text-muted-foreground bg-muted flex items-center justify-between gap-2 border-t px-3 py-1.5 text-[11px]">
          <span>↑↓ navigate · ↵ select · esc close</span>
          <span>
            {toolCount} of {tools.length} tools
          </span>
        </div>
      </PopoverContent>
    </Popover>
  );
}
