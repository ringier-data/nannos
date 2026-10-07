---
name: manage-shared-jobs
description: Share, subscribe to, copy, pause, suspend, reschedule or delete scheduled jobs without surprising anyone — who is affected, which id to use, and which question to ask first. Use when the user wants to share a job with a team, use a job someone shared, change the schedule of a shared job, stop a job, make a job a group default, or asks "why do I get this job?".
---

# Manage shared jobs

Goal: the change the user meant, applied to exactly the people they meant —
asking before anything that affects others.

Ids: a job id (what /app/scheduler/:id shows and scheduler_list_jobs returns) is
the USER'S subscription. Sharing, subscribing, copying, suspending, resetting
schedules and group defaults take the job's DEFINITION id (scheduler_get_job
shows it). Never mix them up; read the job first.

Who is affected — say it before acting:

- pause / resume: only the user's own subscription.
- suspend / unsuspend: every subscriber (needs write). Ask first, naming how many
  subscribers it has.
- delete: an owned job is deleted for EVERY subscriber; a subscribed job only
  unsubscribes the user. Say which one it is before calling scheduler_delete_job.
- schedule change on a job with other subscribers: scheduler_update_job needs
  scope "mine" (only the user's schedule; allowed when the trigger policy is
  overridable) or "everyone" (the job's default). ALWAYS ask which — never assume.
- reset schedules: drops every subscriber's own schedule back to the default.
- follow default schedule: drops only the user's own schedule.

Sharing (scheduler_share_job, needs write):

1. console_list_my_groups for group ids and member counts.
2. The permission list REPLACES the job's group permissions: start from the
   current ones (scheduler_get_job) and add/remove, or you revoke existing access.
3. read = the group may subscribe or copy; write = also edit, suspend, share on.
4. A group that cannot use the job's sub-agent cannot get the job: share the agent
   first (sharing a job never grants agent access).
5. Members gaining or losing access are notified — mention it.

Using a shared job:

- scheduler_list_shared_jobs → subscribe (runs under the user's OWN account and
  credentials, on the job's default schedule in their timezone, delivered to them)
  or copy (an independent job they own, no link back). Explain the difference when
  the user did not choose.
- Trigger policy: overridable (subscribers may set their own schedule; default for
  tasks) or fixed (everyone follows the default; default for watches).

Group defaults (group managers):

- scheduler_add_group_default_job gives every current and future member their own
  subscription. Confirm with the member count first.
- scheduler_remove_group_default_job removes the subscriptions the default created
  (members who subscribed themselves keep theirs); new members no longer get it.

Edge cases:

- "Why do I get this job?" → scheduler_get_job: subscribed by the user, a group
  default, or shared and subscribed earlier; offer unsubscribe (or ask the group
  manager when it is a group default).
- A subscriber's run fails with an auth error → it runs with THEIR credentials; they
  must connect the tool themselves.
- Editing a shared job's definition changes it for everyone: say so before saving.
