import cronstrue from 'cronstrue';

/** Quick-pick presets for the most common schedules. */
export const CRON_PRESETS: { label: string; expr: string }[] = [
  { label: 'Every hour', expr: '0 * * * *' },
  { label: 'Daily 09:00', expr: '0 9 * * *' },
  { label: 'Weekdays 09:00', expr: '0 9 * * 1-5' },
  { label: 'Weekly (Mon)', expr: '0 9 * * 1' },
  { label: 'Monthly (1st)', expr: '0 9 1 * *' },
];

export interface CronDescription {
  /** Whether the expression is a non-empty, valid cron string. */
  ok: boolean;
  /** Human-readable description when ok, error message when invalid, empty when blank. */
  text: string;
}

/** Parse a cron expression into a human-readable description, or an error. */
export function describeCron(expr: string): CronDescription {
  const trimmed = expr.trim();
  if (!trimmed) return { ok: false, text: '' };
  try {
    return {
      ok: true,
      text: cronstrue.toString(trimmed, {
        throwExceptionOnParseError: true,
        use24HourTimeFormat: true,
      }),
    };
  } catch (e) {
    return { ok: false, text: e instanceof Error ? e.message : 'Invalid cron expression' };
  }
}
