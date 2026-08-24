/**
 * Arguments editor for an MCP tool call, generated from the tool's `input_schema`.
 *
 * The scheduler used to ask for `check_args` as a raw JSON textarea, which meant
 * knowing every tool's parameter names by heart and getting no feedback until submit.
 * The gateway already returns a JSON Schema per tool, so the fields can be rendered
 * from it — typed, marked required, with enums as selects — and the JSON textarea
 * kept as an escape hatch for shapes a flat form cannot express.
 */
import { useMemo } from 'react';

import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { McpTool } from '@/api/generated/types.gen';
import { type FlatParam, parseToolSchema } from '@/lib/mcpTools';
import { cn } from '@/lib/utils';

/** Human-readable type line under a parameter name. */
function metaLine(param: FlatParam): string {
  const type =
    param.type === 'multi'
      ? 'multi-select'
      : param.enumValues
        ? param.enumValues.join(' | ')
        : param.type;
  return `${type} · ${param.required ? 'required' : 'optional'}`;
}

export function McpToolArgsFields({
  tool,
  values,
  exprs,
  onChange,
  onExprsChange,
  missingRequired,
}: {
  tool: McpTool | undefined;
  values: Record<string, unknown>;
  /** Per-argument CEL expressions — a value typed with a leading `=`. */
  exprs: Record<string, string>;
  onChange: (next: Record<string, unknown>) => void;
  onExprsChange: (next: Record<string, string>) => void;
  /** Required parameter names to flag after a failed submit. */
  missingRequired?: Set<string>;
}) {
  const { params } = useMemo(() => parseToolSchema(tool), [tool]);

  function set(key: string, value: unknown) {
    const next = { ...values };
    // An emptied field is an unset argument, not an empty string: sending `""`
    // makes tools that validate their input reject the call.
    if (value === '' || value === undefined) delete next[key];
    else next[key] = value;
    onChange(next);
  }

  function setExpr(key: string, expr: string | undefined) {
    const next = { ...exprs };
    if (expr === undefined || expr === '') delete next[key];
    else next[key] = expr;
    onExprsChange(next);
  }

  if (params.length === 0) return null;

  return (
    <div className="bg-muted grid gap-3 rounded-md border p-3">
      {params.map((param) => {
        const raw = values[param.key];
        const invalid = missingRequired?.has(param.key);
        return (
          <div
            key={param.key}
            className="grid gap-1.5 sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)] sm:items-center sm:gap-3"
          >
            <div className="grid gap-0.5">
              <Label htmlFor={`arg-${param.key}`} className="font-mono text-[13px]">
                {param.key}
              </Label>
              <span className={cn('text-xs', invalid ? 'text-destructive' : 'text-muted-foreground')}>
                {invalid ? 'required' : metaLine(param)}
              </span>
            </div>

            {param.type === 'boolean' ? (
              <div className="flex h-9 items-center">
                <Switch
                  id={`arg-${param.key}`}
                  checked={raw === true}
                  onCheckedChange={(checked) => set(param.key, checked ? true : undefined)}
                />
              </div>
            ) : param.type === 'multi' ? (
              // Toggleable chips rather than a dropdown: the whole option set stays
              // visible, and what is picked reads at a glance.
              <div className="flex flex-wrap gap-1.5 py-1">
                {param.enumValues?.map((option) => {
                  const selected = Array.isArray(raw) && raw.includes(option);
                  return (
                    <button
                      key={option}
                      type="button"
                      aria-pressed={selected}
                      className={cn(
                        'rounded-full border px-2.5 py-0.5 font-mono text-[11px] transition-colors',
                        selected
                          ? 'border-primary bg-primary text-primary-foreground'
                          : 'bg-background text-muted-foreground hover:text-foreground',
                      )}
                      onClick={() => {
                        const current = Array.isArray(raw) ? (raw as string[]) : [];
                        const next = selected
                          ? current.filter((v) => v !== option)
                          : [...current, option];
                        // An emptied selection is an unset argument, same as a
                        // cleared text field.
                        set(param.key, next.length ? next : undefined);
                      }}
                    >
                      {option}
                    </button>
                  );
                })}
              </div>
            ) : param.enumValues ? (
              <Select
                value={typeof raw === 'string' ? raw : ''}
                onValueChange={(v) => set(param.key, v)}
              >
                <SelectTrigger id={`arg-${param.key}`} className="w-full bg-background">
                  <SelectValue placeholder="Select…" />
                </SelectTrigger>
                <SelectContent>
                  {param.enumValues.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              (() => {
                // Spreadsheet convention: a value starting with `=` is a CEL
                // expression over `now` and `prev`, resolved fresh on every run —
                // how a rolling date window lives in a date argument.
                const expr = exprs[param.key];
                const shown =
                  expr !== undefined
                    ? `= ${expr}`
                    : raw === undefined || raw === null
                      ? ''
                      : String(raw);
                return (
                  <Input
                    id={`arg-${param.key}`}
                    aria-invalid={invalid || undefined}
                    className={cn('bg-background', expr !== undefined && 'font-mono text-xs')}
                    inputMode={param.type === 'string' || expr !== undefined ? undefined : 'decimal'}
                    value={shown}
                    placeholder={param.placeholder ?? param.description ?? ''}
                    onChange={(e) => {
                      const text = e.target.value;
                      if (text.startsWith('=')) {
                        set(param.key, undefined);
                        setExpr(param.key, text.slice(1).trimStart());
                        return;
                      }
                      setExpr(param.key, undefined);
                      if (param.type === 'string' || text === '') return set(param.key, text);
                      // Keep the raw text while it is not yet a number, so typing "-" or
                      // "1." does not get swallowed; the API validates the final value.
                      const asNumber = Number(text);
                      set(param.key, Number.isFinite(asNumber) && text.trim() !== '' ? asNumber : text);
                    }}
                  />
                );
              })()
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Raw JSON editor, used when a schema has shapes the flat form cannot express. */
export function McpToolArgsJson({
  text,
  onChange,
  error,
}: {
  text: string;
  onChange: (next: string) => void;
  error?: string | null;
}) {
  return (
    <div className="grid gap-1.5">
      <Textarea
        rows={4}
        value={text}
        aria-invalid={Boolean(error) || undefined}
        onChange={(e) => onChange(e.target.value)}
        placeholder='{"campaign_id": "4821"}'
        className="font-mono text-xs"
      />
      {error && <p className="text-destructive text-xs">{error}</p>}
    </div>
  );
}
