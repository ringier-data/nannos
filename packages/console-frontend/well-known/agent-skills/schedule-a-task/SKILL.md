---
name: schedule-a-task
description: Turn "every Monday, do X and send it to me" into a scheduled task job — choose or define the agent that does it, write the per-run instruction, get the schedule right in the user's local time (cron, interval or once), pick delivery, then fill the open job form or create the job. Use when the user wants something done on a schedule or at a later time — "every weekday at 8 send me my agenda", "remind me tomorrow at 9", "each month summarise our spend", "run my report agent every hour".
---

# Schedule a task

Goal: a task job that runs the right agent at the right local time and delivers an
answer the user can use — created through the open form when there is one,
otherwise with scheduler_create_job. (Conditions — "only when X happens" — are a
watch: use set-up-a-watch.)

Steps:

1. One sentence: what runs, when, what the user receives, where. Ask only for what
   you cannot infer.
2. The agent:
   - An existing sub-agent fits → console_list_sub_agents; it must be one the user
     can use (and, if the job will be shared, one the groups can reach).
   - Otherwise define one inline for this job: name, one-line description, model
     tier, system prompt of at most 500 characters, at most 3 MCP tools (find them
     with console_grep_mcp_tools). Inline agents exist only for this job.
3. The instruction (`prompt`): what the agent does on EACH run, self-contained —
   the run has no memory of this chat. Relative words ("today", "this week") are
   resolved at run time, which is what the user wants; absolute dates go stale.
4. The schedule — always in the user's local wall-clock time, never UTC; leave
   timezone unset unless the user names a zone:
   - Recurring at fixed times → cron, 5 fields: "0 8 * * 1-5" = weekdays 08:00,
     "0 9 1 * *" = the 1st of each month 09:00. Read it back in words.
   - Every N minutes/hours → interval (seconds, at least 60).
   - One time ("tomorrow at 9", "in two hours") → once, with run_at: call
     get_current_time first and compute from its answer; it must be in the future.
   - The kind cannot be changed after creation — choose deliberately.
5. Delivery: console_list_delivery_channels, match the user's words; none = in-app.
   A channel only reaches a user who has signed in to Nannos from it once. Voice
   call only if asked (needs a phone number in Settings).
6. Max failures (default 3): the job pauses itself after that many failed runs in a
   row. Keep the default unless the user cares.
7. Write it:
   - New Job form open (/app/scheduler/new) → apply: job type "task" first, then the
     agent (or inline agent fields), instruction, schedule kind, its value, delivery.
     Submit only when the user wants it created.
   - A job's page → invoke `edit` if it is read-only, then apply and submit.
   - No form → summarise in plain words, and on confirmation scheduler_create_job
     (name ≥ 5 characters, job_type "task", sub_agent_id or sub_agent_parameters,
     prompt, schedule fields, delivery_channel_id). Offer to open /app/scheduler/<id>.

Edge cases:

- "Remind me" with nothing to compute → a task whose instruction is to write the
  reminder, delivered where the user wants it; once schedule.
- Cron with day-of-month AND day-of-week set means "either" in cron — avoid it;
  say "the first Monday" needs a different approach (a weekly job whose instruction
  checks the date).
- The user's timezone is wrong in Settings → the job runs at the wrong hour; check
  it when the time matters and point them to Settings → Preferences.
- A paused job does nothing until resumed; a job that paused itself after failures
  has a reason on its page — read it before resuming.
