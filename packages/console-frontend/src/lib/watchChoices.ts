/**
 * The exclusive choices a watch form makes, and what each one means on save.
 *
 * Switching a choice only changes which fields are shown. Every field keeps what was
 * typed into it, so flipping back and forth loses nothing; the choice is applied once,
 * here, when the job is saved. Clearing the hidden half on the switch instead — the
 * first version — lost text on any path that forgot to restore it.
 */

/** How the trigger is decided: an expression, a model's judgement, or both stacked. */
export type ConditionMode = 'cel' | 'judge' | 'both';

/** A notification's text: written by a model from what matched, or sent verbatim. */
export type MessageMode = 'written' | 'fixed';

export interface WatchChoices {
  condition_mode: ConditionMode;
  cel_expr: string;
  llm_condition: string;
  outcome: 'notify' | 'agent';
  message_mode: MessageMode;
  notification_message: string;
  /** The writing brief for a written notification. */
  notification_brief: string;
  /** The sub-agent's instruction. */
  prompt: string;
}

/** The choice a stored job implies, read off which halves it has. */
export function conditionModeOf(value: { cel_expr?: string | null; llm_condition?: string | null }): ConditionMode {
  const cel = Boolean(value.cel_expr?.trim());
  const judge = Boolean(value.llm_condition?.trim());
  return cel && judge ? 'both' : judge ? 'judge' : 'cel';
}

export function messageModeOf(value: { notification_message?: string | null }): MessageMode {
  return value.notification_message?.trim() ? 'fixed' : 'written';
}

/**
 * The values a save sends: what the chosen modes use, trimmed, and empty for everything
 * the choices hide. Empty rather than absent, so each caller maps "not used" onto its own
 * API shape (null on an update, omitted on a create).
 */
export function resolveWatchChoices(value: WatchChoices): {
  cel_expr: string;
  llm_condition: string;
  notification_message: string;
  prompt: string;
} {
  const agent = value.outcome === 'agent';
  const fixed = !agent && value.message_mode === 'fixed';
  return {
    cel_expr: value.condition_mode === 'judge' ? '' : value.cel_expr.trim(),
    llm_condition: value.condition_mode === 'cel' ? '' : value.llm_condition.trim(),
    notification_message: fixed ? value.notification_message.trim() : '',
    // One API field, two meanings: the agent's instruction, or the writer's brief.
    prompt: agent ? value.prompt.trim() : fixed ? '' : value.notification_brief.trim(),
  };
}
