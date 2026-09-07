/**
 * Renders a tool response as clickable JSON, handing back the JSONPath of whatever
 * the user clicks.
 *
 * A watch condition is a JSONPath into a payload the author has usually never seen —
 * the old form asked for `$.status` against a response it never showed. Showing the
 * real payload and letting the value be picked out of it turns that from recall into
 * recognition.
 *
 * Arrays are expanded, because a summarised `[ 3 items ]` tells you nothing about the
 * payload you are meant to write a condition against. Long ones show their first few
 * entries with the rest behind a click, so a hundred-row list cannot bury everything
 * else.
 *
 * Every node is pickable, objects included: a CEL expression is as happy with a subtree
 * as with a scalar, so the old "single value only" mode (which existed for JSONPath
 * equality) has nothing left to mean.
 */
import { useMemo, useState } from 'react';

import { pathSegment } from '@/lib/watchCondition';
import { cn } from '@/lib/utils';

/** Array entries shown before the "show all" row appears. */
const PREVIEW_ITEMS = 3;

/**
 * Hard cap on rendered lines. A tool can return a deeply nested page of records, and
 * the point of this panel is to be readable — past this the payload is not something
 * you pick a value out of by eye.
 */
const MAX_LINES = 400;

interface Line {
  key: string;
  /** Indent depth, one step per nesting level. */
  depth: number;
  /** Rendered `"key": ` or `[0]: ` prefix; empty for closing brackets. */
  label: string;
  /** Rendered value text, including any trailing comma. */
  value: string;
  /** JSONPath of this node, or null when it cannot be selected. */
  path: string | null;
  tone: 'string' | 'number' | 'punctuation';
  /** Set on the "show all"/"show less" row of a long array. */
  toggle?: string;
}

function summarise(value: unknown): { text: string; tone: Line['tone'] } {
  if (typeof value === 'string') return { text: JSON.stringify(value), tone: 'string' };
  return { text: String(value === undefined ? null : JSON.stringify(value)), tone: 'number' };
}

/** Flatten a JSON value into printable lines, in document order. */
function flatten(root: unknown, expanded: Set<string>): Line[] {
  const lines: Line[] = [];
  let truncated = false;

  function push(line: Line): boolean {
    if (lines.length >= MAX_LINES) {
      truncated = true;
      return false;
    }
    lines.push(line);
    return true;
  }

  /** Render one child under `path`, prefixed by `label`. */
  function child(value: unknown, path: string, label: string, comma: string, depth: number) {
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const entries = Object.entries(value as Record<string, unknown>);
      if (entries.length === 0) {
        push({
          key: path,
          depth,
          label,
          value: `{}${comma}`,
          path,
          tone: 'punctuation',
        });
        return;
      }
      if (
        !push({
          key: path,
          depth,
          label,
          value: '{',
          path,
          tone: 'punctuation',
        })
      )
        return;
      entries.forEach(([k, v], i) =>
        child(v, `${path}${pathSegment(k)}`, `"${k}": `, i < entries.length - 1 ? ',' : '', depth + 1)
      );
      push({
        key: `${path}#close`,
        depth,
        label: '',
        value: `}${comma}`,
        path: null,
        tone: 'punctuation',
      });
      return;
    }

    if (Array.isArray(value)) {
      if (value.length === 0) {
        // Pickable: "this list is empty" is a condition people want.
        push({ key: path, depth, label, value: `[]${comma}`, path, tone: 'punctuation' });
        return;
      }
      const showAll = expanded.has(path);
      const shown = showAll ? value.length : Math.min(PREVIEW_ITEMS, value.length);
      if (!push({ key: path, depth, label, value: '[', path, tone: 'punctuation' })) return;
      for (let i = 0; i < shown; i += 1) {
        child(value[i], `${path}[${i}]`, `[${i}]: `, i < value.length - 1 ? ',' : '', depth + 1);
      }
      if (value.length > shown) {
        const remaining = value.length - shown;
        push({
          key: `${path}#more`,
          depth: depth + 1,
          label: '',
          value: `… ${remaining} more ${remaining === 1 ? 'item' : 'items'}`,
          path: null,
          tone: 'punctuation',
          toggle: path,
        });
      } else if (showAll && value.length > PREVIEW_ITEMS) {
        push({
          key: `${path}#less`,
          depth: depth + 1,
          label: '',
          value: 'show fewer',
          path: null,
          tone: 'punctuation',
          toggle: path,
        });
      }
      push({
        key: `${path}#close`,
        depth,
        label: '',
        value: `]${comma}`,
        path: null,
        tone: 'punctuation',
      });
      return;
    }

    const { text, tone } = summarise(value);
    push({ key: path, depth, label, value: `${text}${comma}`, path, tone });
  }

  if (root === null || typeof root !== 'object') {
    // A tool returning a bare scalar is wrapped by the backend, but be defensive.
    const { text, tone } = summarise(root);
    return [{ key: '$', depth: 0, label: '', value: text, path: null, tone }];
  }

  if (Array.isArray(root)) {
    child(root, '$', '', '', 0);
  } else {
    const entries = Object.entries(root as Record<string, unknown>);
    push({ key: '#open', depth: 0, label: '', value: '{', path: null, tone: 'punctuation' });
    entries.forEach(([k, v], i) =>
      child(v, `$${pathSegment(k)}`, `"${k}": `, i < entries.length - 1 ? ',' : '', 1),
    );
    push({ key: '#close', depth: 0, label: '', value: '}', path: null, tone: 'punctuation' });
  }

  if (truncated) {
    lines.push({
      key: '#truncated',
      depth: 0,
      label: '',
      value: `… response too large to show in full (${MAX_LINES}+ lines)`,
      path: null,
      tone: 'punctuation',
    });
  }
  return lines;
}

export function JsonPathPicker({
  value,
  onPick,
}: {
  value: unknown;
  onPick: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const lines = useMemo(() => flatten(value, expanded), [value, expanded]);

  function toggle(path: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  return (
    <div className="bg-background max-h-80 overflow-auto px-1.5 py-2">
      {lines.map((line) => {
        if (line.toggle) {
          return (
            <button
              key={line.key}
              type="button"
              onClick={() => toggle(line.toggle!)}
              style={{ paddingLeft: `${0.5 + line.depth}rem` }}
              className="text-muted-foreground hover:text-foreground block rounded-sm py-px font-mono text-xs leading-5 underline decoration-dotted underline-offset-2"
            >
              {line.value}
            </button>
          );
        }
        const selectable = line.path !== null;
        return (
          <div
            key={line.key}
            role={selectable ? 'button' : undefined}
            tabIndex={selectable ? 0 : undefined}
            title={selectable ? `Watch ${line.path}` : undefined}
            onClick={selectable ? () => onPick(line.path!) : undefined}
            onKeyDown={
              selectable
                ? (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      onPick(line.path!);
                    }
                  }
                : undefined
            }
            style={{ paddingLeft: `${0.5 + line.depth}rem` }}
            className={cn(
              'rounded-sm py-px font-mono text-xs leading-5 whitespace-pre',
              selectable && 'hover:bg-accent cursor-pointer'
            )}
          >
            <span className="text-muted-foreground">{line.label}</span>
            <span
              className={cn(
                line.tone === 'string' && 'text-emerald-700 dark:text-emerald-400',
                line.tone === 'number' && 'text-amber-700 dark:text-amber-400',
                line.tone === 'punctuation' && 'text-muted-foreground'
              )}
            >
              {line.value}
            </span>
          </div>
        );
      })}
    </div>
  );
}
