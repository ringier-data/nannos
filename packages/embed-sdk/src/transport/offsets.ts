/**
 * Code-point offset discipline for streamed reply text.
 *
 * The backend's `turnOffset` / `snapshot.offset` count Unicode CODE POINTS
 * (Python `len`), while JS `String.length` counts UTF-16 units. The two run
 * apart the moment the reply contains an astral-plane character (any emoji),
 * and deriving an offset from `.length` then silently drops chunks. Rule:
 * an applied offset is only ever assigned from a server number, and slicing
 * by offset goes through here.
 */

/** Return the suffix of `full` starting at code point `fromCodePoint`. */
export function sliceFromCodePoint(full: string, fromCodePoint: number): string {
  if (fromCodePoint <= 0) return full;
  let units = 0;
  let points = 0;
  while (units < full.length && points < fromCodePoint) {
    const code = full.codePointAt(units)!;
    units += code > 0xffff ? 2 : 1;
    points += 1;
  }
  return full.slice(units);
}

/** Number of Unicode code points in `s` (what the backend calls its length). */
export function codePointLength(s: string): number {
  let n = 0;
  // for..of iterates by code point, not UTF-16 unit.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  for (const _ of s) n += 1;
  return n;
}
