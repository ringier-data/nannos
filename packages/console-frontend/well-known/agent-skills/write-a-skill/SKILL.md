---
name: write-a-skill
description: Write or improve a skill (SKILL.md) — a trigger-first description, step-by-step instructions, optional bundled files — and put it where it belongs (the registry editor on screen, a sub-agent, or the user's personal or group scope) with the right visibility. Use when the user wants to teach an agent a reusable procedure — "write a skill for our release checklist", "turn this prompt into a skill", "import the xlsx skill", "make this skill available to everyone".
---

# Write a skill

Goal: a skill an agent loads at the right moment and can follow without guessing,
saved at the right scope and visibility.

Steps:

1. Check it does not exist yet: console_search_skills (registry first; source
   "external" for the skills.sh index, "repo:owner/name" for a GitHub repository).
   If a good one exists, offer to activate or import it instead of writing a new one.
2. Name: lowercase letters, digits and single hyphens, e.g. "release-checklist".
3. Description, one line, trigger first: what it does AND when to use it, in the
   words a user would say. Agents only see this line until they load the skill.
4. Instructions (Markdown): Goal, numbered Steps naming the exact tools to call and
   in which order, Edge cases. Concrete over clever; no secrets. Larger reference
   material or scripts go into bundled files (e.g. references/terms.md,
   scripts/check.py) — executable files make the agent run them in a sandbox.
5. Decide where it lives:
   - Registry editor open (/app/skill-registry) → apply the Skill fields (name,
     description, instructions, visibility), then invoke `save` when the user wants it saved.
   - A sub-agent's skill editor open → apply the SubAgentSkill fields there.
   - No form open → console_create_skill with agent_name set explicitly (the target
     sub-agent's exact name, or "orchestrator" for the user's main agent — never the
     default "self", which is you), scope personal | group (with group_id) |
     sub-agent, skill_name, description, body, optional files, visibility.
6. Visibility: private by default. Public makes it findable and usable by every
   console user — and once other agents reference it, it can no longer be made
   private or deleted until they detach. Ask before choosing public.

Edge cases:

- Importing (console_import_skill: repo "owner/repo", skill directory) → report the
  security verdict; do not force an "unsafe" import unless the user insists after
  hearing why it was flagged.
- Updating a skill other agents reference → console_update_skill changes it for
  the calling agent; pinned referrers stay on the old content until they update,
  following referrers get a new version automatically. Say which applies.
- "Always apply this" instructions that are not a procedure → a playbook
  (console_update_playbook) fits better than a skill.
- Sub-agent scope adds the skill to that agent's configuration: it creates a new
  version, which may need approval if it pushes the agent past the auto-approve
  limits (only inlined skills count toward the prompt length).
