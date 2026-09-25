---
status: accepted (2026-09-25), nannos#294; amends ADR-0011 decision 1, closes the gap ADR-0012 recorded
---

# An agent's own skill edit is a config version

A sub-agent's **own** skills are pinned by the content hash in its config version,
exactly like the skills it references (ADR-0011). Every content change of an own
skill writes a config version of the owner: a config save always did, and an edit
made **outside** a config save (the registry UI, `console_update_skill`,
`console_write_skill_file`, `console_delete_skill_file`) now does too, through the
normal auto-approve rules. A version is therefore what the agent ran with, a
revert restores skill content, and no own-skill edit gets past the checks a config
change goes through.

## Context

- ADR-0011 decision 1 made an agent's own row resolve **always-latest**: the
  version stored a `{registry_id, content_hash}` ref, but resolution ignored the
  hash for a row the agent owns. That hid a gap: the out-of-config edit paths all
  end in `SkillRegistryService.update_skill`, which changed the row and its
  snapshot but wrote no config version. The owner's version kept a stale hash,
  invisibly, because the hash was never read.
- Two consequences, both recorded in nannos#294. **Revert did not revert skill
  content**: `revert_to_version` copied the old refs forward and resolution served
  the latest body anyway, so the history was not an audit trail of what the agent
  ran. **Registry edits bypassed every version-level check**: with inlining
  (ADR-0012) an own inlined skill could grow past the auto-approve prompt limit
  through a registry edit, with no approval; ADR-0012 recorded that as a known gap.

## Decision

1. **The owner is pinned by hash.** `resolve_imported_skills_bulk` resolves an own
   row at the version's `content_hash`, like any reference, with `update_available`
   and `latest_hash` when the row has moved on. What stays owner-specific is only
   what is reported around it: the row's visibility, and no activation `mode`.
   `_inlined_skills_length` measures an own bare ref at its pinned hash for the
   same reason: it is what runs.
2. **An out-of-config edit writes the owner's version, in the edit's transaction.**
   `update_skill` calls the owner-edit hook (`SubAgentService.bump_own_skill`) when
   the files of a sub-agent-scoped row change. It is a second hook next to the
   content-changed hook of ADR-0011, not the same one: a config save and a host
   sync reach `_save_version_snapshot` too, through `upsert_agent_skill`, and they
   write their own version. Only `update_skill` is an edit with no version behind
   it.
3. **Built from the approved default, signed by the editor.** Like a following
   bump, the version is the agent's approved default with the one hash replaced,
   so a pending draft is never promoted unreviewed; `current_version` moves past
   such a draft, which survives as a version. The signer differs from ADR-0011
   decision 3: this is the editor's own action, so the editor signs it, and the
   change summary reads `Edited skill '<slug>' <old> -> <new>`. An agent with no
   approved default yet builds from its current version (the same content, so
   the same verdict). An embed-bound agent is skipped: the host owns its versions
   and the next sync re-points the hash (ADR-0006).
4. **The normal auto-approve rules decide.** `_meets_auto_approve_constraints`
   with the inlined length the new skills produce. An AUTOMATED agent over a
   limit **refuses the edit**: `PromptLimitError` propagates out of `update_skill`
   and the registry write rolls back with it. Any other agent's version is left
   pending, and the agent keeps running the previous content until it is
   approved. The MCP tools say so in their reply, and the registry `PUT` returns
   `owner_version {sub_agent_id, version, approved}`.
5. **Migration 107 re-points existing owned refs once, in every version**, to
   the row's current hash. Before this ADR every version served the row's latest
   content for an own skill, so that is what each of them actually ran with; the
   audit trail starts at the migration. Re-pointing only the approved default
   would leave old versions holding hashes that never ran, and a revert to one
   would restore content the agent never had.

## Considered options

- **An edit the user approved in chat counts as approval of the version.** The
  self-improvement flow (HITL in chat) already asks the user; a second approval
  in the console feels redundant for a public agent or one with a long prompt.
  Rejected for now: the chat approval is given by whoever is chatting, and
  auto-approval exists precisely to bound what a version can change without a
  reviewer with write access looking at it. A config save from that same user
  would wait too. The reply tells the agent the version is pending, so nothing
  fails silently. This can be revisited with evidence of friction.
- **Build the owner's version from the current draft.** Would keep a draft and a
  skill edit together, but promotes the draft when the rules pass. Rejected, as
  ADR-0011 rejected it for bumps; the same "long-lived draft and edits do not
  mix" consequence is accepted.
- **Keep the owner always-latest and check the limit in `update_skill` only.**
  Closes the approval gap but not the revert one, and leaves the history a lie.
  Rejected.
- **Pin to the ref as stored and show "update available" instead of migrating.**
  Would silently change what every existing agent runs at deploy time. Rejected.

## Consequences

- One version per own-skill edit. Agents that self-improve often get a longer
  history; the summary names the skill and hashes so it reads as "edited X".
- A config save resends own skill bodies, and `upsert_agent_skill` writes them:
  after a revert, the next config save moves the row back to the reverted
  content, as any edit would. The row is the content of the last write, and a
  config save is a write. Referrers pinned to the newer hash then see "update
  available" for content they already have; following referrers get a bump.
- An own skill can now show "update available" on the agent page: after a
  revert, while an owner version is pending, or when the edited skill was not in
  the approved default. The console shows the badge and the diff, but the
  "update" action stays reference-only: for an own skill the way forward is to
  edit or save the agent, whose config save carries the body.
- `SkillRegistryService.update_skill` returns `SkillUpdateResult` (`entry`,
  `owner_version`) instead of the bare entry.
- `update_skill_hash_in_config` (the MCP `self_update` path) is a no-op when the
  hash is already in place, so the owner hook and a sub-agent-scope activation of
  one's own row compose into one version instead of two.
- ADR-0011 decision 1 is amended: resolution still branches on ownership, but
  only for what is reported, not for which content is served. ADR-0012's known
  gap is closed.
