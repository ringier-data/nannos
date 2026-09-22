-- rambler up

-- ============================================================================
-- Teach the task-scheduler sub-agent that a job can be shared (ADR-0010).
--
-- 101 split a job into a shareable DEFINITION and one SUBSCRIPTION per user,
-- and the REST layer grew the verbs for it — subscribe, copy, share, group
-- default, suspend, reset — every one of them an MCP tool. None of that is in
-- the agent's seeded prompt (075, amended by 085): an agent asked to "share the
-- Monday report with Sales" has the tools and no idea they exist, and the one
-- genuinely ambiguous case ("move the report to 8" on a job four people
-- subscribe to) has no rule to follow.
--
-- Targeted replacements rather than a whole-prompt reseed, in the style of 085,
-- so an operator's edits to the rest of the prompt survive. Each replace() is a
-- no-op once the text is already there, and the WHERE clause keeps this to the
-- system-owned seed.
-- ============================================================================

UPDATE sub_agent_config_versions cv
SET mcp_tools = (
        SELECT jsonb_agg(DISTINCT t)
        FROM jsonb_array_elements(
            COALESCE(cv.mcp_tools, '[]'::jsonb) || '[
                "scheduler_list_shared_jobs",
                "scheduler_subscribe_job",
                "scheduler_unsubscribe_job",
                "scheduler_copy_job",
                "scheduler_share_job",
                "scheduler_suspend_job",
                "scheduler_unsuspend_job",
                "scheduler_reset_job_schedules",
                "scheduler_follow_default_schedule",
                "scheduler_add_group_default_job",
                "scheduler_remove_group_default_job",
                "console_list_my_groups"
            ]'::jsonb
        ) AS t
    ),
    -- Guarded so the up is re-runnable after the down. The down deliberately KEEPS the
    -- prompt text (only mcp_tools go), and every search string below is a substring of
    -- its own replacement — so an ungurded `rambler down` → `rambler up` would insert the
    -- tool list, the `- Sharing:` line and the <sharing> block a SECOND time. The tools
    -- assignment above is left unguarded on purpose: the down really did remove those,
    -- and re-adding them is idempotent (`jsonb_agg(DISTINCT …)`).
    system_prompt = CASE WHEN cv.system_prompt LIKE '%<sharing>%' THEN cv.system_prompt ELSE replace(
        replace(
            replace(
                cv.system_prompt,
-- 1. The tool list.
'- scheduler_delete_job: Permanently delete a scheduled job',
'- scheduler_delete_job: Permanently delete a scheduled job
- scheduler_list_shared_jobs: List jobs shared with the user (and the org-wide templates) that they could subscribe to or copy
- scheduler_subscribe_job: Activate a shared job for the user — it then runs under THEIR account
- scheduler_unsubscribe_job: Remove only the user''s own activation of a shared job
- scheduler_copy_job: Copy a shared job into an independent one the user owns
- scheduler_share_job: Give groups read (may subscribe) or write (may edit) on a job the user owns
- scheduler_add_group_default_job / scheduler_remove_group_default_job: Activate a shared job for EVERY member of a group, or stop doing so
- scheduler_suspend_job / scheduler_unsuspend_job: Stop or restart a job for every subscriber (owner/writer action)
- scheduler_reset_job_schedules: Put every subscriber back on the job''s default schedule
- scheduler_follow_default_schedule: Put the USER back on the job''s default schedule, dropping the one of their own — their half of the reset, needing no permission
- console_list_my_groups: The user''s groups with member counts — resolve a group NAME to an id before sharing, and quote the count when you confirm'
            ),
-- 2. The responsibilities line, so sharing is part of the job from the top.
'- User Guidance: Help users refine their scheduling requirements and notification preferences',
'- Sharing: Share a job with a group, subscribe to one shared with the user, copy one to diverge from it, or make one a group''s default
- User Guidance: Help users refine their scheduling requirements and notification preferences'
            ),
-- 3. The rules. Placed before <best_practices>, which every version of this
--    prompt has kept.
'<best_practices>',
'<sharing>
A job has two halves. The DEFINITION is what the job is — its prompt or watch check, its
agent, its condition, its default schedule — owned by one user and shareable to groups. A
SUBSCRIPTION is one person''s activation of it: whether it is on for them, the schedule
they actually run on, where their results go. Every subscription runs under ITS OWN
subscriber''s identity, credentials and spend, so five subscribers means five runs. Do not
offer to "send one run to several people" — that is not what sharing does.

The user never has to hear any of this. Say "the job", and mention the split only when
something the user asked for actually depends on it.

Sharing a job does NOT grant access to the sub-agent it runs: if the group cannot reach
the agent, share the agent first — scheduler_share_job will refuse otherwise.

Subscribe vs copy: subscribe keeps following the author''s version (their later fixes
arrive); copy makes an independent job the user owns and nothing propagates to it. Ask
which they mean only when it is genuinely unclear; "I want my own version" is a copy.

Granting on behalf of other people — scheduler_share_job, and above all
scheduler_add_group_default_job, which switches the job ON for every member of the group
under their own identity — always needs the user''s explicit confirmation first. Resolve
the group with console_list_my_groups and say the number out loud: "This will activate
''Monday report'' for all 12 members of Sales. Go ahead?" Match the plural to the count —
a group of one gets "the 1 member of Sales", never "all 1 members".

Changing the schedule of a job with OTHER subscribers is the one ambiguous edit. Pass
scope on scheduler_update_job: ''mine'' changes only this user''s schedule, ''everyone''
changes the job''s default for every subscriber who has not customised theirs (and needs
write permission). When there are other subscribers and the user has not said which they
meant, ASK — and ASK BEFORE CALLING THE TOOL, never by setting one schedule and then
asking: a schedule you set and undo leaves the user with a schedule of their own where
they had none, which stops the owner''s later changes reaching them.
"Put me back on the normal time", "follow the default again" or undoing a change you just
made is scheduler_follow_default_schedule — for the user alone, no permission needed.
Doing it to EVERY subscriber is scheduler_reset_job_schedules and needs write. Sending a
null schedule to scheduler_update_job does NOT clear one.
With a single subscriber the two are the same thing and scope is pointless.

Stopping a job: scheduler_pause_job stops it for the user only; scheduler_suspend_job
stops it for EVERYONE (owner/writer action, and each member''s own on/off choice is
remembered for when it resumes). Never reach for suspend when the user said "stop sending
me this" — that is unsubscribe, or pause.

"Why am I getting this?" is answerable: scheduler_get_job reports who owns the job and
whether the user subscribed themselves or a group default did it for them.
</sharing>

<best_practices>'
    ) END,
    -- The agent card description, which is what the orchestrator routes on. Guarded for
    -- the same reason as the prompt above.
    description = CASE WHEN cv.description LIKE '%Share jobs with groups%' THEN cv.description ELSE replace(
        cv.description,
        '- List, view, update, pause, or resume existing schedules',
        '- List, view, update, pause, or resume existing schedules
- Share jobs with groups, subscribe to shared jobs, or make one a group''s default'
    ) END
FROM sub_agents sa
WHERE cv.sub_agent_id = sa.id
  AND sa.name = 'task-scheduler'
  AND sa.owner_user_id = 'system';

-- rambler down

-- The tools go; the prompt text stays. Removing the <sharing> block would have
-- to find it again in a prompt an operator may since have edited, and a
-- paragraph describing tools the agent no longer holds costs a few tokens and
-- misleads nobody — it reads as capability it cannot reach, which is what the
-- tool list already tells it.
UPDATE sub_agent_config_versions cv
SET mcp_tools = (
        SELECT COALESCE(jsonb_agg(t), '[]'::jsonb)
        FROM jsonb_array_elements(COALESCE(cv.mcp_tools, '[]'::jsonb)) AS t
        WHERE t NOT IN (
            '"scheduler_list_shared_jobs"'::jsonb,
            '"scheduler_subscribe_job"'::jsonb,
            '"scheduler_unsubscribe_job"'::jsonb,
            '"scheduler_copy_job"'::jsonb,
            '"scheduler_share_job"'::jsonb,
            '"scheduler_suspend_job"'::jsonb,
            '"scheduler_unsuspend_job"'::jsonb,
            '"scheduler_reset_job_schedules"'::jsonb,
            '"scheduler_follow_default_schedule"'::jsonb,
            '"scheduler_add_group_default_job"'::jsonb,
            '"scheduler_remove_group_default_job"'::jsonb,
            '"console_list_my_groups"'::jsonb
        )
    )
FROM sub_agents sa
WHERE cv.sub_agent_id = sa.id
  AND sa.name = 'task-scheduler'
  AND sa.owner_user_id = 'system';
