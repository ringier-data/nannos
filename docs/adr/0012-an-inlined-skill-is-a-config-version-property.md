---
status: accepted (2026-09-25), nannos#291; amends ADR-0006, builds on ADR-0011
---

# An inlined skill is a config-version property

A sub-agent can mark any skill in its config as **inlined**, whether it owns
the skill or references it (ADR-0011). An inlined skill's full SKILL.md goes
into the system prompt on every turn instead of waiting for a `load_skill`
call. The flag is `inline` on the `SkillRef` stored in the config version, not
on the `skill_activations` row. Turning it on is a config change like any
other: it goes through draft and approval, and a revert restores the old
choice. The model then has the skill without an extra round trip, and it
can't forget to load it.

## Context

- #290 let a host that publishes the agent definition (ADR-0006) name
  skills in `x-nannos-agent.skills_inline`. The sync pasted their bodies into
  `system_prompt`. That worked only for embed-bound agents, and the skill text
  ended up in two places: the prompt and the skill list.
- ADR-0011 put the skill's **mode** (pinned / following) on the activation
  row, because mode is a relationship between two agents and must survive a
  revert. Inline is different: it changes what the agent's own prompt is.
  Also, an agent's own skills have no activation row, so a flag stored there
  would not exist for them.

## Decision

1. **`SkillRef.inline: bool = false`**, in the config version's `skills`
   JSONB, set by a config save. The activate payload (`inline`, sub-agent
   scope only) is a shortcut that writes it into the version the activation
   creates. Mode and inline are independent: mode decides when the content
   hash moves, inline decides where the content goes.
2. **Runtime.** An inlined skill is removed from the "Available skills" list
   and rendered in its own `## Inlined skills` block, as the exact text
   `load_skill` would return, including the list of bundled files.
   `load_skill` still returns it, and bundled files stay under `/skills/`. A
   personal or group skill that overrides an inlined skill by name is inlined
   in its place.
3. **Auto-approval counts it.** Effective prompt length is `system_prompt`
   plus every inlined body, checked against the same auto-approve limit.
   - **Config save:** the rule is unchanged: an AUTOMATED agent over the limit
     is rejected, and a LOCAL agent's version waits for approval.
   - **Inlined activation:** an AUTOMATED agent over the limit gets a 422, and
     a LOCAL agent's version is left pending.
   - **Following bump:** a bump that would exceed the limit is recorded as a
     failed bump and does not land.
   - **Host sync:** exempt, as its prompt already is.
4. **The host marks inline in the skill itself.** It sets `metadata:
   {nannos-inline: true}` in the SKILL.md frontmatter, next to
   `nannos-visibility`. `x-nannos-agent.skills_inline` is removed without a
   deprecation period. The sync copies the metadata to `SkillRef.inline`, and
   the console shows it read-only on bound agents. The metadata applies only
   to the bound agent: another agent that references the published skill
   makes its own choice.

## Considered options

- **Flag on the activation row, like mode.** Rejected: an agent's own
  skills have no row, and a revert would not restore the prompt the agent
  actually ran with.
- **Keep pasting text at sync time (#290).** Rejected: it works only for
  bound agents and keeps two copies of the skill text in the version.
- **`inline` on the `index.json` `skills[]` entry.** Rejected: that object is
  the RFC's shape. Nannos keys go in `x-nannos-agent` or in SKILL.md
  `metadata`, and the SKILL.md digest already covers the metadata, so it is
  part of the revision without extra work.
- **Leave inlining out of the auto-approve check.** The content is one
  `load_skill` call away anyway. Rejected for consistency: the check measures
  the prompt the model runs with.

## Consequences

- **Breaking for hosts.** A host that still publishes `skills_inline` loses
  inlining without an error, because unknown fields are ignored. Its build is
  updated in the same change. `FRAMING_TEMPLATE_VERSION` goes to `"2"`, so
  every bound agent re-syncs once and gets its prompt without the pasted
  text.
- **Followed skills can fall behind.** An inlined, following skill whose
  publisher grows it past the limit stays on the old hash and shows "update
  available". This needs the failed-bump notification ADR-0011 left as a
  follow-up.
- **Host order no longer matters.** Inlined skills render sorted by name.
- **Overrides are not measured.** A personal or group override of an inlined skill
  is inlined with a body no auto-approve check has seen. That was chosen on purpose
  (decision 2): the override only reaches the prompt in the sessions of the user or
  group that wrote it, and that content could already be loaded with `load_skill`.
- **No document store needed.** Without a store, a runtime still resolves the
  config's own skills, so an inlined host skill reaches the prompt, as #290's
  pasted text always did.
