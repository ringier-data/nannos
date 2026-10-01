/**
 * Reading a watch job's check arguments out of whichever editor is authoritative.
 *
 * There are two: a form generated from the tool's `input_schema`, and a raw JSON
 * textarea for shapes a flat form cannot express. Both the fields themselves and the
 * page's submit validation need to resolve them the same way, so the resolution lives
 * here rather than in either.
 */
import type { McpTool } from '@/api/generated/types.gen';
import { parseToolSchema } from '@/lib/mcpTools';

export interface WatchArgsValue {
  check_args: Record<string, unknown>;
  check_args_text: string;
  args_mode: 'fields' | 'json';
  /** Per-argument CEL expressions (`= …` in the form), resolved at call time. */
  check_args_exprs: Record<string, string>;
}

/**
 * Which arguments editor can show all of `args`.
 *
 * The field form only renders the scalars the tool's schema declares, so an argument
 * outside that set would be submitted without ever being visible. Both draft-application
 * paths (the create dialog and the job detail page) need the same answer; the detail
 * page's copy hardcoded 'fields' and so reintroduced exactly that.
 */
export function argsModeFor(
  args: Record<string, unknown>,
  tool: McpTool | undefined,
  exprs: Record<string, string>,
): 'fields' | 'json' {
  // An expression lives in its argument's field, so it needs one as much as a value does.
  const renderable = new Set(parseToolSchema(tool).params.map((param) => param.key));
  return [...Object.keys(args), ...Object.keys(exprs)].every((key) => renderable.has(key)) ? 'fields' : 'json';
}

/**
 * The JSON editor's text for a set of arguments: values as they are, expressions as
 * `"= …"` strings — the fields' own convention.
 *
 * The expressions are part of the call, so an editor showing only the values reads as a
 * call without them: a rolling `created_after` window displayed as `{}`.
 */
export function argsText(args: Record<string, unknown>, exprs: Record<string, string>): string {
  const all: Record<string, unknown> = { ...args };
  for (const [key, expr] of Object.entries(exprs)) all[key] = `= ${expr}`;
  return Object.keys(all).length ? JSON.stringify(all, null, 2) : '';
}

/**
 * The JSON editor's text split into values and expressions: a top-level string starting
 * with `=` is an expression, as it is in a field. Only the top level — a nested string is
 * data the tool receives, and the scheduler resolves expressions per argument.
 */
export function parseArgsText(text: string): {
  args: Record<string, unknown> | undefined;
  exprs: Record<string, string>;
  error?: string;
} {
  const trimmed = text.trim();
  if (!trimmed) return { args: undefined, exprs: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { args: undefined, exprs: {}, error: 'Arguments are not valid JSON.' };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { args: undefined, exprs: {}, error: 'Arguments must be a JSON object.' };
  }
  const args: Record<string, unknown> = {};
  const exprs: Record<string, string> = {};
  for (const [key, raw] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof raw === 'string' && raw.startsWith('=')) {
      // trimStart, as the field does: the same `= …` text is the same expression.
      const expr = raw.slice(1).trimStart();
      // A bare `=` sets nothing, as in a field.
      if (expr) exprs[key] = expr;
    } else {
      args[key] = raw;
    }
  }
  return { args: Object.keys(args).length > 0 ? args : undefined, exprs };
}

/**
 * The arguments and expressions to send, or the reason the raw JSON cannot be used.
 *
 * In the JSON editor both halves come from its text. `check_args_exprs` there is a copy
 * kept for the views that read state, and a copy is only as fresh as the last edit: text
 * filled in from a stored job or a draft has never been through one.
 */
export function resolveArgs(value: WatchArgsValue): {
  args: Record<string, unknown> | undefined;
  exprs: Record<string, string>;
  error?: string;
} {
  if (value.args_mode === 'fields') {
    return {
      args: Object.keys(value.check_args).length > 0 ? value.check_args : undefined,
      exprs: value.check_args_exprs,
    };
  }
  const { args, exprs, error } = parseArgsText(value.check_args_text);
  // Mid-edit JSON keeps the last good expressions, so the preview does not blink.
  return error ? { args: undefined, exprs: value.check_args_exprs, error } : { args, exprs };
}

/**
 * The arguments editor for a set of arguments: its JSON text, and the mode that can show
 * all of them. Every path that fills the form from outside — a draft, an AI edit, a
 * stored job — goes through this, so they cannot drift apart.
 */
export function argsView(
  args: Record<string, unknown>,
  exprs: Record<string, string>,
  tool: McpTool | undefined,
): Pick<WatchArgsValue, 'check_args_text' | 'args_mode'> {
  return { check_args_text: argsText(args, exprs), args_mode: argsModeFor(args, tool, exprs) };
}

/**
 * Required arguments the schema declares but nothing has been given for.
 *
 * Only meaningful while the generated fields are in use: in raw JSON the author is
 * deliberately outside the schema, and second-guessing them there would be noise.
 */
export function missingRequiredArgs(
  tool: McpTool | undefined,
  value: WatchArgsValue,
  args: Record<string, unknown> | undefined,
): Set<string> {
  if (value.args_mode === 'json') return new Set();
  return new Set(
    parseToolSchema(tool)
      .params.filter((p) => p.required)
      .map((p) => p.key)
      .filter((key) => {
        // An argument supplied as an expression is supplied.
        if (value.check_args_exprs[key]?.trim()) return false;
        const given = args?.[key];
        return given === undefined || given === null || given === '';
      }),
  );
}

/** The part of a watch that decides what the check tool returns. */
export interface CheckCall {
  check_tool?: string | null;
  check_args?: Record<string, unknown> | null;
  check_args_exprs?: Record<string, unknown> | null;
}

/**
 * Whether two calls would get the same response shape: same tool, same arguments, same
 * `= …` expressions. An empty object and a missing one are the same call.
 *
 * A stored response describes the call that produced it. Once the form's call differs,
 * testing an expression against it fails on fields the new call does return.
 */
export function sameCheckCall(a: CheckCall, b: CheckCall): boolean {
  const key = (c: CheckCall) =>
    JSON.stringify([
      c.check_tool || null,
      c.check_args && Object.keys(c.check_args).length ? c.check_args : null,
      c.check_args_exprs && Object.keys(c.check_args_exprs).length ? c.check_args_exprs : null,
    ]);
  return key(a) === key(b);
}
