/**
 * Minimal YAML emitter for the developer chrome (wire badges, dev inspector) —
 * JSON is too space-hungry to read in a narrow panel. Output is for EYES: it
 * aims to be valid YAML (quoting where a plain scalar would misparse, block
 * scalars for multiline text), but nothing round-trips it, and clipboard
 * exports stay JSON on purpose. No dependency: the payloads are plain wire
 * JSON, so a hand emitter covers everything that can occur.
 *
 * The emitter produces TOKENS (`toYamlTokens`) so the dev views can highlight
 * without re-parsing the text — a regex highlighter over generated output
 * would misread block-scalar prose as keys. `toYaml` is the same token stream
 * joined to a string.
 */

export type YamlTokenType = 'punct' | 'key' | 'str' | 'num' | 'bool' | 'null' | 'text';

export interface YamlToken {
  t: YamlTokenType;
  s: string;
}

/** One rendered line, indentation included as a leading punct token. */
export type YamlLine = YamlToken[];

const INDENT = '  ';

const punct = (s: string): YamlToken => ({ t: 'punct', s });
const indent = (depth: number): YamlToken => punct(INDENT.repeat(depth));

/** Control chars a literal block cannot carry (everything but \n). */
// eslint-disable-next-line no-control-regex
const UNPRINTABLE = /[\u0000-\u0008\u000b-\u001f\u007f]/;

/** Safe as a PLAIN (unquoted) scalar. Deliberately conservative: any ':' or
 *  ' #' quotes the whole string (timestamps, URLs), which stays compact and
 *  never misparses. */
function isPlain(s: string): boolean {
  if (s.length === 0) return false;
  if (UNPRINTABLE.test(s) || s.includes('\n')) return false;
  if (s.includes(':') || s.includes(' #')) return false;
  if (/^[-?,[\]{}#&*!|>'"%@`\s]/.test(s)) return false;
  if (/\s$/.test(s)) return false;
  if (/^(true|false|null|yes|no|on|off|~)$/i.test(s)) return false;
  if (/^[+-]?(\d|\.\d)/.test(s)) return false;
  return true;
}

function scalarToken(value: unknown): YamlToken {
  if (value === null || value === undefined) return { t: 'null', s: 'null' };
  if (typeof value === 'boolean') return { t: 'bool', s: String(value) };
  if (typeof value === 'number' || typeof value === 'bigint') return { t: 'num', s: String(value) };
  const s = typeof value === 'string' ? value : String(value);
  return { t: 'str', s: isPlain(s) ? s : JSON.stringify(s) };
}

/** Multiline text renders as a `|-` block — the whole point of the switch. */
function isBlockText(value: unknown): value is string {
  return typeof value === 'string' && value.includes('\n') && !UNPRINTABLE.test(value);
}

function keyPrefix(k: string): YamlToken[] {
  return [{ t: 'key', s: isPlain(k) ? k : JSON.stringify(k) }, punct(':')];
}

/** Object entries minus `undefined` values, mirroring JSON.stringify. */
function defined(value: object): Array<[string, unknown]> {
  return Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== undefined);
}

/** Lines for `prefix` + value at `depth`. `prefix` is a key + ':' or a '-'. */
function entry(
  prefix: YamlToken[],
  value: unknown,
  depth: number,
  seen: WeakSet<object>,
): YamlLine[] {
  const lead = [indent(depth), ...prefix];
  if (isBlockText(value)) {
    const body = value.replace(/\n+$/, '').split('\n');
    return [
      [...lead, punct(' |-')],
      ...body.map((line): YamlLine => [indent(depth + 1), { t: 'text', s: line }]),
    ];
  }
  if (value === null || typeof value !== 'object') {
    return [[...lead, punct(' '), scalarToken(value)]];
  }
  if (seen.has(value)) return [[...lead, punct(' '), { t: 'str', s: '"[circular]"' }]];
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      if (value.length === 0) return [[...lead, punct(' []')]];
      return [lead, ...value.flatMap((v) => seqItem(v, depth + 1, seen))];
    }
    const entries = defined(value);
    if (entries.length === 0) return [[...lead, punct(' {}')]];
    return [lead, ...entries.flatMap(([k, v]) => entry(keyPrefix(k), v, depth + 1, seen))];
  } finally {
    seen.delete(value);
  }
}

/** One sequence item. Objects fold compactly onto the dash: `- key: value`. */
function seqItem(value: unknown, depth: number, seen: WeakSet<object>): YamlLine[] {
  if (value !== null && typeof value === 'object' && !Array.isArray(value) && !seen.has(value)) {
    const entries = defined(value);
    if (entries.length > 0) {
      seen.add(value);
      try {
        const lines = entries.flatMap(([k, v]) => entry(keyPrefix(k), v, depth + 1, seen));
        // '- ' is exactly one INDENT wide, so the fold keeps children aligned.
        // Every entry line leads with its indent token — swap the first one.
        const [first, ...rest] = lines;
        return [[punct(INDENT.repeat(depth) + '- '), ...first.slice(1)], ...rest];
      } finally {
        seen.delete(value);
      }
    }
  }
  return entry([punct('-')], value, depth, seen);
}

/** Render any JSON-ish value as highlightable lines. `undefined` → '—'. */
export function toYamlTokens(value: unknown): YamlLine[] {
  if (value === undefined) return [[{ t: 'text', s: '—' }]];
  if (value === null || typeof value !== 'object') {
    if (isBlockText(value)) {
      return value.split('\n').map((line): YamlLine => [{ t: 'text', s: line }]);
    }
    return [[scalarToken(value)]];
  }
  const seen = new WeakSet<object>();
  seen.add(value); // the root itself can be the cycle
  const lines = Array.isArray(value)
    ? value.length === 0
      ? [[punct('[]')]]
      : value.flatMap((v) => seqItem(v, 0, seen))
    : defined(value).length === 0
      ? [[punct('{}')]]
      : defined(value).flatMap(([k, v]) => entry(keyPrefix(k), v, 0, seen));
  // Depth-0 lines lead with an empty indent token; renderers need none of it.
  return lines.map((line) => line.filter((token) => token.s !== ''));
}

/** Render any JSON-ish value as YAML text — the token stream, joined. */
export function toYaml(value: unknown): string {
  return toYamlTokens(value)
    .map((line) => line.map((token) => token.s).join(''))
    .join('\n');
}
