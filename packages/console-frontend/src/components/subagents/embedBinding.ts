/**
 * Shared helpers for the embed binding forms (ADR-0006).
 */

/** Split a textarea of client ids on newlines and commas, trimmed and de-duplicated. */
export function parseAzps(raw: string): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const part of raw.split(/[\n,]/)) {
    const azp = part.trim();
    if (azp && !seen.has(azp)) {
      seen.add(azp);
      result.push(azp);
    }
  }
  return result;
}
