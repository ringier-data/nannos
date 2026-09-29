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
   the same verdict). An own skill that only a pending draft holds (added in the
   draft) gets a draft built from that draft instead, never approved by the
   edit: left at the old hash, the draft's next config save would write the old
   body back over the edit. An embed-bound agent's own-skill edit is refused:
   the host owns its skills (ADR-0006), the edit would never run, and the next
   sync would overwrite it.
4. **The normal auto-approve rules decide.** `_meets_auto_approve_constraints`
   with the inlined length the new skills produce. An AUTOMATED agent over a
   limit **refuses the edit**: `PromptLimitError` propagates out of `update_skill`
   and the registry write rolls back with it. Any other agent's version is left
   pending as a draft to submit for approval, and the agent keeps running the
   previous content until it is approved. The MCP tools say so in their reply,
   and the registry `PUT` and the single-file write and delete return
   `owner_version {sub_agent_id, version, approved}`.
5. **A revert, and setting a default version, write the agent's own skill rows
   back** to the content that version pins (`_restore_own_skill_rows`), the way a
   config save writes them.
   Without it the row keeps the newer content, and the next edit outside a config
   save (MCP file tools, registry UI) builds on it and brings the reverted content
   back with no warning. A revert is therefore an edit of the skill: following
   referrers are bumped to the reverted content, pinned ones see "update
   available". Chosen over building out-of-config edits from the approved
   version's pinned content, which would leave the row and the agent disagreeing
   and make every edit path resolve the pin first.
6. **Migration 114 re-points existing owned refs once, in every version**, to
   the row's current hash. Before this ADR every version served the row's latest
   content for an own skill, so that is what each of them actually ran with; the
   audit trail starts at the migration. Re-pointing only the approved default
   would leave old versions holding hashes that never ran, and a revert to one
   would restore content the agent never had. It also snapshots every row's
   current content: a pinned ref resolves through the snapshot of its hash, and
   rows written before every write was snapshotted would otherwise fall back to
   the row's newer content once edited.

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
- A config save resends own skill bodies, and `upsert_agent_skill` writes them;
  a revert writes them too (decision 5). The row is the content of the last
  write. Referrers pinned to the newer hash then see "update available";
  following referrers get a bump.
- An own skill can now show "update available" on the agent page: while an
  owner version is pending, or when the edited skill was not in the approved
  default. The console shows the badge and the diff, but the
  "update" action stays reference-only: for an own skill the way forward is to
  edit or save the agent, whose config save carries the body.
- `SkillRegistryService.update_skill` returns `SkillUpdateResult` (`entry`,
  `owner_version`) instead of the bare entry.
- The MCP `self_update` writes no version for the agent's own row: the owner
  hook has decided it, so the hook and a sub-agent-scope activation of one's own
  row compose into one version instead of two, and a pending draft is not
  approved behind the hook's back.
- A single-file write or delete (`update_skill(edit_files=...)`) derives the new
  file set from the row as read under its lock, so it cannot drop a concurrent
  write's content.
- ADR-0011 decision 1 is amended: resolution still branches on ownership, but
  only for what is reported, not for which content is served. ADR-0012's known
  gap is closed.
- Lock order: every writer of a version locks the agent before it writes one
  of the agent's own registry rows (config save, host sync, own-skill edit),
  and an own-skill edit reads the row again under that lock, so its hash gate
  sees what a config save it waited behind wrote. The order does not cover
  following bumps: two agents that follow each other's skills can still
  deadlock when both are edited at the same time, as two config saves of such
  a pair already could. The victim is the follower bump, which fails in its
  savepoint: the edit commits, the follower keeps the old hash, and the
  deadlock is recorded in its `last_bump_error`.
- An edit back to the content the approved default already pins writes no
  version. If a pending version that pins other content is current, it stops
  being current: `current_version` returns to the approved default, and the
  pending version survives in the history.
