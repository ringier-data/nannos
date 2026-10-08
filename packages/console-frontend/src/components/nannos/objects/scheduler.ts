import { z } from 'zod';
import type { FieldBridge, ObjectTypeRegistry } from '@nannos/embed-sdk';
import { config } from '@/config';
import { describeCron } from '@/lib/cron';
import { SUB_AGENT_NAME_HINT, SUB_AGENT_NAME_MAX, SUB_AGENT_NAME_RE } from '@/lib/subAgentName';
import { argsText, parseArgsText } from '@/lib/watchArgs';

/**
 * Field contracts shared by the create dialog (`ScheduledJob`) and the job detail
 * page's edit form (`ExistingScheduledJob`). Both forms hold the same value shapes —
 * ids as strings, the interval as a numeric string, run_at as datetime-local text —
 * so one schema per field serves both.
 */
const fields = {
  name: z.string().min(1).describe('Job name, shown in the job list and in notifications.'),

  cron_expr: z
    .string()
    .refine((v) => describeCron(v).ok, 'Not a valid cron expression')
    .describe(
      'Standard 5-field cron expression (minute hour day-of-month month day-of-week), e.g. "0 9 * * 1-5" for 09:00 on weekdays. ' +
        "Interpreted in the user's timezone (their settings timezone on create; the job's stored timezone on edit), not UTC. " +
        'Only used when schedule_kind is "cron".',
    ),
  interval_seconds: z
    .string()
    .regex(/^\d+$/, 'Whole seconds, digits only')
    .refine((v) => Number(v) >= 60, 'At least 60 seconds')
    .describe(
      'Seconds between runs, as a string of digits (e.g. "3600" for hourly). Minimum "60". Only used when schedule_kind is "interval".',
    ),
  run_at: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/, 'Format YYYY-MM-DDTHH:mm')
    .describe(
      'When a one-time job runs, as local wall-clock time "YYYY-MM-DDTHH:mm" (no seconds, no offset, no Z), e.g. "2026-10-07T14:30". ' +
        "Interpreted in the user's timezone; must be in the future. Only used when schedule_kind is \"once\".",
    ),

  // ── What runs (task jobs, and watch jobs whose outcome is "agent") ─────────
  sub_agent_mode: z
    .enum(['existing', 'automated'])
    .describe(
      '"existing" runs a sub-agent that already exists (set sub_agent_id). "automated" defines a one-off agent inline for this job ' +
        '(set automated_name, automated_description, automated_system_prompt, automated_model, and optionally automated_mcp_tools).',
    ),
  sub_agent_id: z
    .string()
    .regex(/^\d+$/, 'Numeric id as a string')
    .describe(
      'Numeric id of an existing sub-agent the user can use, as a string (e.g. "12"). Used when sub_agent_mode is "existing". ' +
        'The voice agent cannot be chosen here; use voice_call instead.',
    ),
  automated_name: z
    .string()
    .max(SUB_AGENT_NAME_MAX)
    .regex(SUB_AGENT_NAME_RE, SUB_AGENT_NAME_HINT)
    .describe(
      `Name of the inline-defined agent (sub_agent_mode "automated"), an identifier: starts with a letter, then only letters, digits, "-" and "_" — no spaces; max ${SUB_AGENT_NAME_MAX} characters. E.g. "weekly-report-agent".`,
    ),
  automated_description: z
    .string()
    .min(1)
    .max(200)
    .describe('One-line description of what the inline agent does (sub_agent_mode "automated"). Max 200 characters.'),
  automated_system_prompt: z
    .string()
    .min(1)
    .max(config.autoApprove.maxSystemPromptLength)
    .describe(
      `System prompt of the inline agent (sub_agent_mode "automated"): its standing role and task. Max ${config.autoApprove.maxSystemPromptLength} characters.`,
    ),
  automated_model: z
    .string()
    .min(1)
    .describe(
      'Model of the inline agent. Prefer a capability tier: "tier:standard" (default), "tier:low" (cheaper, faster) or "tier:premium" (most capable) — ' +
        'a tier follows the fleet default and survives model upgrades. Otherwise a concrete model alias registered on the gateway; ' +
        'an alias that is not registered falls back to "tier:standard".',
    ),
  automated_mcp_tools: z
    .array(z.string().min(1))
    .max(config.autoApprove.maxMcpToolsCount)
    .describe(
      `Full names of the MCP tools the inline agent may call, exactly as the tool catalogue lists them. Replaces the whole list. Max ${config.autoApprove.maxMcpToolsCount}; may be empty.`,
    ),
  automated_enable_thinking: z
    .boolean()
    .describe(
      'Extended thinking for the inline agent. Only offered when automated_model is a concrete claude* or gemini* alias (never for a tier); leave false otherwise.',
    ),
  automated_thinking_level: z
    .enum(['minimal', 'low', 'medium', 'high'])
    .describe('Reasoning effort when automated_enable_thinking is true; ignored otherwise.'),
  prompt: z
    .string()
    .describe(
      'Instruction sent to the sub-agent on each run (optional). For a task job: the specific task for this run — empty means the agent follows its own ' +
        'configuration. For a watch job with outcome "agent": what to do with what the condition matched, which the agent receives alongside. ' +
        'Not the notification text — for that see notification_message / notification_brief.',
    ),

  // ── Watch jobs ─────────────────────────────────────────────────────────────
  check_tool: z
    .string()
    .min(1)
    .describe(
      'Full name of the MCP tool the watch calls on every run, exactly as the tool catalogue lists it; its response is what the condition is evaluated on. ' +
        'Set it before check_args and cel_expr: changing it does NOT clear existing arguments or the expression, so set those again for the new tool.',
    ),
  check_args: z
    .string()
    .refine((v) => !parseArgsText(v).error, 'Must be a JSON object')
    .describe(
      'Arguments for check_tool as a JSON object string, e.g. \'{"status": "open", "limit": 20}\'; "" for no arguments. Replaces all arguments. ' +
        'A top-level string value starting with "=" is a CEL expression evaluated on every run instead of a fixed value ' +
        "(variables: now — current time in the job's timezone; prev — the previous run's response), " +
        'e.g. \'{"since": "= strftime(now - duration(\\"168h\\"), \\"%Y-%m-%d\\")"}\' for a rolling 7-day window. ' +
        'Keys should match the tool\'s input schema.',
    ),
  condition_mode: z
    .enum(['cel', 'judge', 'both'])
    .describe(
      'How the watch decides it has triggered. "cel": cel_expr alone (deterministic, free). "judge": a small model reads the whole response and ' +
        'decides by llm_condition. "both": cel_expr filters first, then the model judges only what it matched. ' +
        'The field the mode does not use keeps its text but is not saved.',
    ),
  cel_expr: z
    .string()
    .describe(
      'CEL expression over `result` (the tool response), `now` and `prev` (the previous response). A boolean triggers when true; anything else ' +
        'triggers when non-empty — prefer returning the matching items (e.g. `result.items.filter(i, i.status == "failed")`), since they are what ' +
        'the notification is written from and what an agent receives. Used when condition_mode is "cel" or "both".',
    ),
  llm_condition: z
    .string()
    .describe(
      'Plain-language condition a model judges, e.g. "an attendee looks external to the company". Used when condition_mode is "judge" (over the ' +
        'whole response) or "both" (over what cel_expr matched).',
    ),
  destroy_after_trigger: z
    .boolean()
    .describe('true: the job pauses itself after it triggers once. false: it keeps watching and reports every time the condition holds.'),
  outcome: z
    .enum(['notify', 'agent'])
    .describe(
      'What happens when the watch triggers. "notify": send a notification (see message_mode). "agent": run a sub-agent with what matched ' +
        '(see sub_agent_mode, sub_agent_id, prompt); its reply is delivered instead of a notification. Exclusive.',
    ),
  message_mode: z
    .enum(['written', 'fixed'])
    .describe(
      'For outcome "notify". "written": a model writes the message from what matched, following notification_brief. ' +
        '"fixed": notification_message is sent verbatim every time.',
    ),
  notification_brief: z
    .string()
    .describe(
      'For message_mode "written": brief for the model writing the notification — what to include, how to build links from item fields, layout. ' +
        'Optional; empty yields one or two sentences on what changed.',
    ),
  notification_message: z
    .string()
    .describe(
      'For message_mode "fixed": the exact text sent each time. No placeholders — {x}, {{x}} and ${x} are refused because nothing is substituted; ' +
        'use message_mode "written" to include matched data.',
    ),

  // ── Delivery ───────────────────────────────────────────────────────────────
  voice_call: z
    .boolean()
    .describe('Deliver the outcome as a phone call via the voice agent instead of a message.'),
  delivery_channel: z
    .string()
    .regex(/^(\d+)?$/, 'Numeric id as a string, or empty')
    .describe(
      'Numeric id of the delivery channel (e.g. a Slack or Google Chat bot) results are sent through, as a string (e.g. "3"); "" for in-app notifications only.',
    ),
};

/**
 * The JSON text is the authoritative arguments editor; the form keeps three copies
 * (structured values, `= …` expressions, raw text) plus which editor is showing. The
 * agent sees one JSON string: reads render the active editor's arguments, writes switch
 * the form to the JSON editor with that text and keep the parsed halves in step.
 */
const checkArgsBridge: FieldBridge = {
  read: (form) =>
    form.get('args_mode') === 'json'
      ? form.get('check_args_text')
      : argsText(
          (form.get('check_args') ?? {}) as Record<string, unknown>,
          (form.get('check_args_exprs') ?? {}) as Record<string, string>,
        ),
  write: (value, form) => {
    const text = String(value);
    const { args, exprs } = parseArgsText(text);
    form.set('args_mode', 'json');
    form.set('check_args_text', text);
    form.set('check_args', args ?? {});
    form.set('check_args_exprs', exprs);
  },
  // Served only where the form maps the arguments editor (a viewer who can't edit the
  // job's definition gets none of it).
  available: (form) => !form.has || form.has('check_args_text'),
};

const highlightLabels = {
  name: 'Name',
  cron_expr: 'Cron expression',
  interval_seconds: 'Interval',
  run_at: 'Run at',
  automated_model: 'Model',
  automated_description: 'Description',
  automated_system_prompt: 'System prompt',
  automated_enable_thinking: 'Enable extended thinking',
  check_tool: 'Tool',
  llm_condition: 'AI judges',
  destroy_after_trigger: 'Pause the job after it triggers once',
  notification_brief: 'How to write it',
  notification_message: 'Text to send',
  voice_call: 'phone call',
};

export const schedulerObjectTypes = {
  /** The create dialog on the Scheduler page. */
  ScheduledJob: {
    singular: 'Scheduled job',
    idShape: 'simple-numeric',
    schema: z.object({
      name: fields.name,
      job_type: z
        .enum(['task', 'watch'])
        .describe(
          '"task": run a sub-agent on the schedule. "watch": on the schedule, call check_tool and evaluate a condition; only when it holds, ' +
            'notify or run a sub-agent. Set this first — it decides which other fields apply. Task jobs use sub_agent_mode/sub_agent_id/automated_*/prompt; ' +
            'watch jobs use check_tool, check_args, condition_mode, cel_expr, llm_condition, destroy_after_trigger, outcome and the message fields.',
        ),
      schedule_kind: z
        .enum(['cron', 'interval', 'once'])
        .describe(
          '"cron": recurring by cron_expr. "interval": every interval_seconds. "once": a single run at run_at. Set the matching field too. ' +
            'Cannot be changed after the job is created.',
        ),
      cron_expr: fields.cron_expr,
      interval_seconds: fields.interval_seconds,
      run_at: fields.run_at,
      sub_agent_mode: fields.sub_agent_mode,
      sub_agent_id: fields.sub_agent_id,
      automated_name: fields.automated_name,
      automated_description: fields.automated_description,
      automated_system_prompt: fields.automated_system_prompt,
      automated_model: fields.automated_model,
      automated_mcp_tools: fields.automated_mcp_tools,
      automated_enable_thinking: fields.automated_enable_thinking,
      automated_thinking_level: fields.automated_thinking_level,
      prompt: fields.prompt,
      check_tool: fields.check_tool,
      check_args: fields.check_args,
      condition_mode: fields.condition_mode,
      cel_expr: fields.cel_expr,
      llm_condition: fields.llm_condition,
      destroy_after_trigger: fields.destroy_after_trigger,
      outcome: fields.outcome,
      message_mode: fields.message_mode,
      notification_brief: fields.notification_brief,
      notification_message: fields.notification_message,
      voice_call: fields.voice_call,
      delivery_channel: fields.delivery_channel,
    }),
    overrides: { check_args: checkArgsBridge },
    highlightLabels,
  },

  /**
   * The edit form on a job's detail page. Its job type and schedule kind are fixed, and
   * what is registered follows what the viewer may change: definition fields only for a
   * writer, the schedule only when the trigger is theirs to move. An absent (undefined)
   * field is one this job or this viewer cannot edit.
   */
  ExistingScheduledJob: {
    singular: 'Scheduled job',
    idShape: 'simple-numeric',
    schema: z.object({
      name: fields.name,
      max_failures: z
        .number()
        .int()
        .min(1)
        .max(20)
        .describe('Consecutive failed runs after which the job pauses itself (1–20).'),
      cron_expr: fields.cron_expr.describe(
        `${fields.cron_expr.description} Present only on a cron job whose schedule the user may change.`,
      ),
      interval_seconds: fields.interval_seconds.describe(
        `${fields.interval_seconds.description} Present only on an interval job whose schedule the user may change.`,
      ),
      run_at: fields.run_at.describe(
        `${fields.run_at.description} Present only on a one-time job whose schedule the user may change.`,
      ),
      sub_agent_mode: fields.sub_agent_mode.describe(
        `${fields.sub_agent_mode.description} Watch jobs only (outcome "agent"); a task job's agent is always an existing one.`,
      ),
      sub_agent_id: fields.sub_agent_id,
      automated_name: fields.automated_name,
      automated_description: fields.automated_description,
      automated_system_prompt: fields.automated_system_prompt,
      automated_model: fields.automated_model,
      automated_mcp_tools: fields.automated_mcp_tools,
      automated_enable_thinking: fields.automated_enable_thinking,
      automated_thinking_level: fields.automated_thinking_level,
      prompt: fields.prompt,
      check_tool: fields.check_tool,
      check_args: fields.check_args,
      condition_mode: fields.condition_mode,
      cel_expr: fields.cel_expr,
      llm_condition: fields.llm_condition,
      destroy_after_trigger: fields.destroy_after_trigger,
      outcome: fields.outcome,
      message_mode: fields.message_mode,
      notification_brief: fields.notification_brief,
      notification_message: fields.notification_message,
      voice_call: fields.voice_call,
      delivery_channel: fields.delivery_channel.describe(
        `${fields.delivery_channel.description} The viewer's own: on a shared job, changing it moves only their runs.`,
      ),
    }),
    overrides: { check_args: checkArgsBridge },
    highlightLabels: { ...highlightLabels, max_failures: 'Max failures' },
  },
} satisfies ObjectTypeRegistry;
