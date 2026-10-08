---
name: set-up-a-watch
description: Turn "tell me when …" into a working watch job — find the check tool and its input schema, build the arguments, choose an expression or AI judgement, pick notify or agent and the delivery channel, then fill the open job form or create the job. Use when the user wants to be alerted or have something done when a condition holds — "notify me when a PR is waiting for my review", "alert me if a new bug report comes in", "ping me on Slack when the budget passes 80%", "watch my calendar for external attendees".
---

# Set up a watch

Goal: a watch job that calls one MCP tool on a schedule, triggers only when the
condition holds, and delivers something useful — created through the open form
when there is one, otherwise with scheduler_create_job.

Steps:

1. Pin down the intent in one sentence: what is checked, how often, what counts as
   "it happened", and what the user wants then (a message, or an agent doing
   something). Ask only for what you cannot infer.
2. Find the check tool. console_grep_mcp_tools with a few words from the intent
   ("pull request review", "calendar events"); narrow with server_slug from
   console_list_mcp_servers if results are noisy. Pick the tool whose response
   contains the data the condition needs, and use its name exactly as returned.
3. Read its input schema (and output schema, when present). Build the arguments as
   a JSON object with only schema keys. Anything time-relative becomes an
   expression argument evaluated on every run, never a literal date:
   - in the form: a value starting with "=", e.g. "= strftime(now - duration(\"24h\"), \"%Y-%m-%d\")"
   - in scheduler_create_job: check_args_exprs, e.g. {"since": "strftime(now - duration('24h'), '%Y-%m-%d')"}
4. Choose the condition:
   - Measurable (counts, dates, status values, thresholds, "new since last run")
     → expression (CEL) over result, now, prev. Return the matching items, e.g.
     result.items.filter(i, i.state == "open"), so the message can name them.
   - Write it defensively. Many tools leave a list OUT of the response when it is
     empty (a search with no hits answers {}), and prev is null on the first run:
     guard with has(), e.g.
     has(result.threads) ? result.threads.filter(t, prev == null || !has(prev.threads) || !prev.threads.exists(p, p.id == t.id)) : []
   - Decide whether the same item may trigger twice. A recurring watch whose
     condition only says "there is something" (size(result.items) > 0) re-alerts
     every run for as long as the item exists — an unread email, an open PR. Unless
     the user wants a reminder each time, keep only items not in prev (compare ids).
   - Needs reading comprehension (tone, intent, "looks external") → AI judgement,
     a short plain-language condition.
   - Both → expression first to cut the response down, then AI judges what is left.
5. Choose the outcome:
   - notify, written (default choice): a model writes the message from what
     matched; give a brief if the user wants specific fields or links. In
     scheduler_create_job the brief is `prompt` with notification_message empty.
   - notify, fixed: the same text every time; no placeholders.
   - agent: a sub-agent acts on what matched (sub_agent_id, or an inline agent via
     sub_agent_parameters: name, description, model_tier, system_prompt ≤ 500
     chars, ≤ 3 tools). `prompt` then tells it what to do.
6. Schedule: interval for polling (at least 60 s; pick what the use case needs, not
   the minimum) or cron in the user's local time. Leave timezone unset.
   "Pause after it triggers once" on for a one-off alert, off for ongoing alerts.
7. Delivery: console_list_delivery_channels and match the user's words ("Slack",
   "Google Chat"); none = in-app only. Voice call only if asked.
8. Write it:
   - The New Job form (or a job's edit form) is open → apply, in this order: job
     type "watch", schedule, check tool, arguments, condition mode, expression /
     AI condition, outcome, message fields, delivery. A job's page in view mode →
     invoke its `edit` action first.
   - Then invoke `run_check` (alone) and read what came back: does the expression
     work on the real response, and is the verdict what the user expects? Fix and
     re-run until it is. Fix the expression, never the intent: a CEL error means a
     wrong field, type or function (`timestamp()` not `time()`, a missing `has()`),
     not that a filter should go. "Assigned to me" stays a test on the user's own
     login — read it off the response (e.g. assignees[].login) or ask for it, never
     a literal "me", and never widen it to "has any assignee" to make the error
     disappear. If the tool is not known to be read-only, run_check does
     not call it — ask the user to click "Run it anyway" in the form. When the user
     wants it created, invoke the form's `save` (they approve it).
   - No form open → summarise the job in plain words, and on confirmation call
     scheduler_create_job (job_type "watch", a name of at least 5 characters,
     check_tool, check_args, cel_expr and/or llm_condition, schedule fields,
     delivery_channel_id). Then offer to open /app/scheduler/<id>.

Edge cases:

- No tool returns the needed data → say so; do not fake it with a different tool
  or an AI judgement over unrelated data.
- The tool pages its results → make the arguments fetch a window larger than the
  poll interval and de-duplicate via prev, or items are missed.
- The user wants the alert text to quote matched data → written message, not fixed.
- The expression is rejected on save → it did not compile; fix the syntax (string
  literals in double quotes inside JSON need escaping) rather than switching to AI.
- A watch the user edits is open on its detail page → change it through that form
  (invoke `edit` if it is read-only), never with scheduler_update_job.
- Describe the behaviour honestly: "alerts once per new email" only if the
  expression de-duplicates via prev; "keeps alerting while unread" otherwise.
