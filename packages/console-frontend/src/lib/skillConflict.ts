/**
 * ADR-0011: publishing a skill is a commitment. Deleting a referenced registry row, or
 * making it private, is refused with a 409 whose detail names the referring agents:
 *
 *   { detail: { message: string, referrers: [{ sub_agent_id: number, name: string }] } }
 *
 * `getErrorMessage` only understands a string `detail`, so without this the console shows
 * a bare "Failed to save skill" and the publisher never learns who is holding the row.
 */

export interface SkillReferrer {
  sub_agent_id: number;
  name: string;
}

export interface SkillReferencedConflict {
  message: string;
  referrers: SkillReferrer[];
}

/** Dig the referenced-skill conflict out of whatever shape the SDK threw. */
export function parseSkillReferencedConflict(error: unknown): SkillReferencedConflict | null {
  if (!error || typeof error !== 'object') return null;

  const candidates: unknown[] = [];
  const err = error as Record<string, unknown>;
  candidates.push(err.detail);
  if (err.data && typeof err.data === 'object') {
    candidates.push((err.data as Record<string, unknown>).detail);
  }
  if (err.response && typeof err.response === 'object') {
    const response = err.response as Record<string, unknown>;
    if (response.data && typeof response.data === 'object') {
      candidates.push((response.data as Record<string, unknown>).detail);
    }
  }

  for (const detail of candidates) {
    if (!detail || typeof detail !== 'object') continue;
    const d = detail as Record<string, unknown>;
    if (!Array.isArray(d.referrers)) continue;
    const referrers = d.referrers.filter(
      (r): r is SkillReferrer =>
        !!r && typeof r === 'object' && typeof (r as SkillReferrer).name === 'string'
    );
    if (referrers.length === 0) continue;
    return {
      message: typeof d.message === 'string' ? d.message : 'This skill is in use by another agent.',
      referrers,
    };
  }
  return null;
}

/** One name, "a and b", or "a, b and c" — for a one-line toast description. */
export function formatReferrerNames(referrers: SkillReferrer[]): string {
  const names = referrers.map((r) => r.name);
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/**
 * Toast description for a refused withdrawal, or null when the error is something else.
 * `action` reads as "…before this skill can be deleted".
 */
export function describeSkillReferencedConflict(
  error: unknown,
  action: 'deleted' | 'made private'
): string | null {
  const conflict = parseSkillReferencedConflict(error);
  if (!conflict) return null;
  const who = formatReferrerNames(conflict.referrers);
  const plural = conflict.referrers.length === 1 ? 'agent still uses' : 'agents still use';
  return `${conflict.referrers.length} ${plural} it: ${who}. They must stop using it before this skill can be ${action}.`;
}
