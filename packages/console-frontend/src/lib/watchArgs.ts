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

/** The arguments to send, or the reason the raw JSON cannot be used. */
export function resolveArgs(value: WatchArgsValue): {
  args: Record<string, unknown> | undefined;
  error?: string;
} {
  if (value.args_mode === 'fields') {
    return { args: Object.keys(value.check_args).length > 0 ? value.check_args : undefined };
  }
  const text = value.check_args_text.trim();
  if (!text) return { args: undefined };
  try {
    const parsed = JSON.parse(text);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return { args: undefined, error: 'Arguments must be a JSON object.' };
    }
    return { args: parsed as Record<string, unknown> };
  } catch {
    return { args: undefined, error: 'Arguments are not valid JSON.' };
  }
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
