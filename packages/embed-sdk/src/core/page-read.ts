/**
 * What a page hands back when the agent asks to read the screen
 * (`client_action` kind `read_current_page`).
 *
 * Free-form, because the point of asking is to get what a fixed shape cannot
 * carry: the rows on screen, the filters, an unsaved form. It is answered ON
 * DEMAND rather than sent with every question, so it may be much larger than
 * the per-send page context — but it is still user data going to a model, so
 * `sanitizeReadResult` applies the same deny list at EVERY depth, plus depth,
 * width and length caps.
 *
 * Adapted from Gatana's assistant/page-context.ts read half. The screen-outline
 * DOM walk (core/screen-outline.ts) joins the read via
 * `sanitizeReadResultWithScreen`, which gives it the budget the readers left.
 */

/** A host-registered answer source: whatever the page holds, sync or async. */
export type NannosPageReader = () => unknown | Promise<unknown>;

/**
 * A read is asked for once, and only when the answer is needed, so it may be
 * larger than the snapshot every question carries. Still a ceiling: an
 * over-long read is truncated, not refused — a partial view of the screen is
 * more use to the agent than an error.
 */
const MAX_READ_SERIALIZED = 10_000;
/** How deep a page's own state may nest before the rest is dropped as noise. */
const MAX_READ_DEPTH = 6;
const MAX_READ_ARRAY = 50;
const MAX_READ_KEYS = 40;
const MAX_READ_STRING = 500;
const MAX_READ_KEY = 60;

/** Same rule as the page-context sanitizer: only words that NAME a secret. */
const SECRET_KEY = /secret|token|password|passphrase|api[-_]?key|private[-_]?key|bearer/i;

function clamp(value: string, max: number): string {
  const trimmed = value.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

/**
 * The end of a log or an event stream, within a budget. The newest lines are
 * the ones that say why something is failing, so the budget is spent from the
 * end backwards — a single enormous line cannot push out everything before it.
 * For hosts building readers over logs/streams.
 */
export function tailLines(
  lines: readonly string[],
  maxChars = 3500,
  maxLines = 40,
): { lines: string[]; omitted: number } {
  const kept: string[] = [];
  let spent = 0;
  for (let index = lines.length - 1; index >= 0 && kept.length < maxLines; index -= 1) {
    const line = lines[index].slice(0, MAX_READ_STRING);
    if (spent + line.length > maxChars && kept.length) {
      break;
    }
    kept.unshift(line);
    spent += line.length;
  }
  return { lines: kept, omitted: lines.length - kept.length };
}

/** Walk arbitrary page state and keep only what is safe to show a model. */
function pruneRead(value: unknown, depth: number): unknown {
  if (value === null || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    // NaN and Infinity serialize to null anyway; make that explicit rather than surprising.
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === 'string') {
    return clamp(value, MAX_READ_STRING);
  }
  // A function, a symbol or undefined has nothing to say about the screen. A
  // Date is the one class worth keeping — pages hold timestamps as Dates and
  // JSON would lose them.
  if (value instanceof Date) {
    return value.toISOString();
  }
  if (typeof value !== 'object') {
    return undefined;
  }
  if (depth >= MAX_READ_DEPTH) {
    return undefined;
  }
  if (Array.isArray(value)) {
    return value.slice(0, MAX_READ_ARRAY).map((entry) => pruneRead(entry, depth + 1));
  }
  const pruned: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(value as Record<string, unknown>).slice(0, MAX_READ_KEYS)) {
    // The deny list applies at every depth, not only at the top: a page hands
    // back whatever object it is holding, and the nested part is where a
    // fetched credential would sit.
    if (SECRET_KEY.test(key)) {
      continue;
    }
    const safe = pruneRead(entry, depth + 1);
    if (safe !== undefined) {
      pruned[clamp(key, MAX_READ_KEY)] = safe;
    }
  }
  return pruned;
}

/**
 * Make a page's answer to `read_current_page` safe to send: the deny list plus
 * depth, width and length caps. Returns the JSON the result carries, already
 * serialized, so the cap is measured on what is actually sent.
 */
export function sanitizeReadResult(value: unknown): string {
  const pruned = pruneRead(value, 0);
  if (pruned === undefined) {
    return 'null';
  }
  let serialized: string;
  try {
    serialized = JSON.stringify(pruned) ?? 'null';
  } catch {
    // A cycle survives pruning, because pruning copies rather than tracks what it has seen.
    return JSON.stringify({ error: 'This page reported state that could not be serialized.' });
  }
  if (serialized.length > MAX_READ_SERIALIZED) {
    return JSON.stringify({
      truncated: true,
      note: `The page reported more state than fits. The first ${MAX_READ_SERIALIZED} characters of it follow as text.`,
      partial: serialized.slice(0, MAX_READ_SERIALIZED),
    });
  }
  return serialized;
}

/**
 * The screen outline's share of a read. The readers are measured first and the
 * outline takes what is left: on a page with no reader it gets most of the
 * budget, and on a page whose reader carries log tails it shrinks rather than
 * pushing the whole read over the cap. The floor keeps a long-winded reader
 * from silencing the outline entirely; the ceiling keeps a plain page from
 * spending the whole read on furniture.
 */
const MIN_SCREEN_CHARS = 1500;
const MAX_SCREEN_CHARS = 7000;
/** Serializing the outline escapes its newlines and quotes, so its budget leaves room for that. */
const SCREEN_SLACK = 400;

/**
 * `sanitizeReadResult` for a read that also carries the screen outline.
 *
 * The outline goes in whole, under `screen`, rather than through `pruneRead`:
 * it is one long markdown string, and the per-string cap would cut it at
 * {@link MAX_READ_STRING} characters. Its own budget is the cap that applies
 * instead — computed here, after the readers are measured, which is why the
 * outline is taken as a callback rather than as a string. Should the total
 * still land over the ceiling, the outline is what gets cut, because the
 * readers were registered by hand and the outline was not. `screen` is
 * therefore a reserved reader key.
 */
export function sanitizeReadResultWithScreen(
  answers: unknown,
  snapshot: (maxChars: number) => string,
): string {
  const prunedRaw = pruneRead(answers, 0);
  // The outline joins the answers as a sibling key, so anything that is not a
  // plain object (a custom `readCurrentPage` may return one) is wrapped first.
  const pruned: Record<string, unknown> =
    prunedRaw !== undefined && typeof prunedRaw === 'object' && prunedRaw !== null && !Array.isArray(prunedRaw)
      ? (prunedRaw as Record<string, unknown>)
      : prunedRaw === undefined
        ? {}
        : { state: prunedRaw };
  let spent: number;
  try {
    spent = JSON.stringify(pruned)?.length ?? 2;
  } catch {
    // A cycle survives pruning, because pruning copies rather than tracks what it has seen.
    return JSON.stringify({ error: 'This page reported state that could not be serialized.' });
  }

  let screen = '';
  try {
    screen = snapshot(Math.max(MIN_SCREEN_CHARS, Math.min(MAX_SCREEN_CHARS, MAX_READ_SERIALIZED - spent - SCREEN_SLACK)));
  } catch {
    // The walk failing must not cost the model the readers' answers; it just reads less.
  }

  // The outline is spread last, so a reader that registered under the reserved key loses to it.
  const result: Record<string, unknown> = screen ? { ...pruned, screen } : pruned;
  let serialized = JSON.stringify(result) ?? '{}';
  if (serialized.length > MAX_READ_SERIALIZED && screen) {
    const overshoot = serialized.length - MAX_READ_SERIALIZED;
    result.screen = `${screen.slice(0, Math.max(0, screen.length - overshoot - 24))}…`;
    serialized = JSON.stringify(result) ?? '{}';
  }
  if (serialized.length > MAX_READ_SERIALIZED) {
    return JSON.stringify({
      truncated: true,
      note: `The page reported more state than fits. The first ${MAX_READ_SERIALIZED} characters of it follow as text.`,
      partial: serialized.slice(0, MAX_READ_SERIALIZED),
    });
  }
  return serialized;
}
