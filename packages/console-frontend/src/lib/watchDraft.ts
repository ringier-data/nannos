/**
 * An AI edit of an existing watch: what is sent as the job, and how the answer lands.
 *
 * The draft endpoint answers an edit with the whole job, the change applied. Only the
 * fields that differ from what was sent are written back into the form, so a field the
 * edit did not touch keeps whatever the form holds — including the text behind a hidden
 * choice, which the job sent to the model never contained.
 */
import type { ScheduledJobDraft } from '@/api/generated/types.gen';
import type { WatchFieldsValue } from '@/components/WatchFields';
import type { McpTool } from '@/api/generated/types.gen';
import { argsModeFor, resolveArgs } from '@/lib/watchArgs';
import { conditionModeOf, messageModeOf, resolveWatchChoices } from '@/lib/watchChoices';

/** The fields an edit may change — the backend's `_EDITABLE_DRAFT_FIELDS`. */
const EDITABLE = [
  'check_tool',
  'check_args',
  'check_args_exprs',
  'cel_expr',
  'llm_condition',
  'notification_message',
  'prompt',
  'sub_agent_id',
  'destroy_after_trigger',
] as const;
type Editable = (typeof EDITABLE)[number];

/**
 * The job as the form would save it: only what the choices use, as `resolveWatchChoices`
 * sends it. The model edits the job that would run, not the text behind a hidden choice.
 */
export function draftOfWatch(value: WatchFieldsValue): ScheduledJobDraft {
  const chosen = resolveWatchChoices(value);
  // A JSON editor left mid-edit has no args to send; the model then edits without them.
  const { args } = resolveArgs(value);
  const agentId = value.outcome === 'agent' && value.sub_agent_mode === 'existing' ? value.sub_agent_id : '';
  return {
    job_type: 'watch',
    check_tool: value.check_tool || null,
    check_args: args && Object.keys(args).length > 0 ? args : null,
    check_args_exprs: Object.keys(value.check_args_exprs).length > 0 ? value.check_args_exprs : null,
    cel_expr: chosen.cel_expr || null,
    llm_condition: chosen.llm_condition || null,
    notification_message: chosen.notification_message || null,
    prompt: chosen.prompt || null,
    sub_agent_id: agentId ? parseInt(agentId) : null,
    destroy_after_trigger: value.destroy_after_trigger,
  };
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/**
 * A text field after the edit. A new value is written and badged. A removal clears the
 * field only while it is still shown: behind a choice the edit switched away from, the
 * text stays, as it does when the choice is switched by hand — but a field still shown
 * is still sent, so keeping it there would undo the removal on save.
 */
function written(
  differs: boolean,
  edited: string | null | undefined,
  current: string,
  shown: boolean,
  changed: Set<string>,
  badge: string,
): string {
  if (!differs) return current;
  if (edited) {
    changed.add(badge);
    return edited;
  }
  return shown ? '' : current;
}

/**
 * Write the fields the edit changed into the form, and name them for the AI badges.
 *
 * An inline sub-agent being defined is not something the sent job can express, so an
 * edit never touches the outcome while one is: it would read the missing agent as "no
 * agent" and flip the job to a notification.
 */
export function applyDraftEdit(
  value: WatchFieldsValue,
  sent: ScheduledJobDraft,
  edited: ScheduledJobDraft,
  tools: McpTool[],
): { next: WatchFieldsValue; changed: Set<string> } {
  const differs = new Set<Editable>(EDITABLE.filter((key) => !same(sent[key], edited[key])));
  if (value.outcome === 'agent' && value.sub_agent_mode === 'automated') {
    differs.delete('sub_agent_id');
    differs.delete('prompt');
    differs.delete('notification_message');
  }
  const next = { ...value };
  const changed = new Set<string>();

  if (differs.has('check_tool')) {
    next.check_tool = edited.check_tool ?? '';
    changed.add('check_tool');
  }
  if (differs.has('check_args') || differs.has('check_tool')) {
    const args = (edited.check_args ?? {}) as Record<string, unknown>;
    next.check_args = args;
    next.check_args_text = Object.keys(args).length ? JSON.stringify(args, null, 2) : '';
    next.args_mode = argsModeFor(args, tools.find((t) => t.name === next.check_tool));
    if (differs.has('check_args')) changed.add('check_args');
  }
  if (differs.has('check_args_exprs')) {
    next.check_args_exprs = (edited.check_args_exprs ?? {}) as Record<string, string>;
    changed.add('check_args');
  }

  if (differs.has('cel_expr') || differs.has('llm_condition')) {
    // Read off the edited job, not the form: a half the edit removed must not stay
    // chosen, and one it added must not land behind a hidden choice.
    if (edited.cel_expr || edited.llm_condition) next.condition_mode = conditionModeOf(edited);
    const mode = next.condition_mode;
    next.cel_expr = written(differs.has('cel_expr'), edited.cel_expr, next.cel_expr, mode !== 'judge', changed, 'cel_expr');
    next.llm_condition = written(
      differs.has('llm_condition'),
      edited.llm_condition,
      next.llm_condition,
      mode !== 'cel',
      changed,
      'llm_condition',
    );
  }

  if (differs.has('sub_agent_id') || differs.has('prompt') || differs.has('notification_message')) {
    if (edited.sub_agent_id != null) {
      next.outcome = 'agent';
      next.sub_agent_mode = 'existing';
      next.sub_agent_id = String(edited.sub_agent_id);
      if (differs.has('sub_agent_id')) changed.add('sub_agent_id');
      next.prompt = written(differs.has('prompt'), edited.prompt, next.prompt, true, changed, 'prompt');
    } else {
      // One stored field, two meanings: without an agent `prompt` is the writer's brief.
      next.outcome = 'notify';
      next.message_mode = messageModeOf(edited);
      const fixed = next.message_mode === 'fixed';
      next.notification_message = written(
        differs.has('notification_message'),
        edited.notification_message,
        next.notification_message,
        fixed,
        changed,
        'notification_message',
      );
      next.notification_brief = written(
        differs.has('prompt'),
        edited.prompt,
        next.notification_brief,
        !fixed,
        changed,
        'notification_brief',
      );
    }
  }

  if (differs.has('destroy_after_trigger') && edited.destroy_after_trigger != null) {
    next.destroy_after_trigger = edited.destroy_after_trigger;
    changed.add('destroy_after_trigger');
  }
  return { next, changed };
}
