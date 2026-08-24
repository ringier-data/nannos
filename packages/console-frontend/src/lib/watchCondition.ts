/**
 * Vocabulary for watch conditions, shared by the create dialog and the job detail page.
 *
 * A condition is a CEL expression (extracts the evidence and gates the trigger in
 * one), a model judgement over the response, or both stacked — the gate runs first
 * and the model judges only what the expression returned.
 */

/**
 * One path segment, spelled so JSONPath and CEL read it the same way.
 *
 * `.key` only works for a bare identifier: in CEL `result.content-type` is subtraction,
 * `result.2` does not parse, and a key containing a dot silently becomes two segments —
 * so a click seeded a condition that could never compile, with no hint that the bracket
 * form was needed. Bracket-quoting is valid in both languages, which is what lets
 * jsonPathToCel stay a prefix swap.
 */
export function pathSegment(key: string): string {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) ? `.${key}` : `[${JSON.stringify(key)}]`;
}

/**
 * Turn a picked JSONPath into the CEL spelling of the same location, so clicking a
 * value in the response result seeds the expression instead of doing nothing.
 * `$.a.b[0]` addresses the tool response, which CEL sees as `result`.
 */
export function jsonPathToCel(path: string): string {
  if (!path.startsWith('$')) return path;
  return `result${path.slice(1)}`;
}

/**
 * Break a one-line expression on its logical seams: before `&&`/`||`, and before the
 * branches of a ternary, indented by paren depth. Heuristic on purpose — it never
 * touches strings and only adds line breaks, so the expression means exactly the same
 * thing. Generated expressions arrive as one long line, and a condition that cannot be
 * read cannot be reviewed.
 */
export function formatCel(expr: string): string {
  const flat = expr.replace(/\s+/g, ' ').trim();
  if (flat.length <= 60) return flat;

  let out = '';
  let depth = 0;
  let quote: string | null = null;
  for (let i = 0; i < flat.length; i++) {
    const ch = flat[i];
    if (quote) {
      out += ch;
      if (ch === quote && flat[i - 1] !== '\\') quote = null;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      out += ch;
      continue;
    }
    if (ch === '(' || ch === '[' || ch === '{') depth++;
    if (ch === ')' || ch === ']' || ch === '}') depth--;

    const two = flat.slice(i, i + 2);
    if ((two === '&&' || two === '||') && out.length > 0) {
      out = out.trimEnd() + '\n' + '  '.repeat(Math.max(depth, 1)) + two + ' ';
      i += two.length; // skip the operator and the space that usually follows
      if (flat[i] === ' ') i++;
      i--;
      continue;
    }
    if ((ch === '?' || ch === ':') && depth === 0 && flat[i + 1] === ' ' && flat[i - 1] === ' ') {
      out = out.trimEnd() + '\n  ' + ch + ' ';
      if (flat[i + 1] === ' ') i++;
      continue;
    }
    out += ch;
  }
  return out;
}
