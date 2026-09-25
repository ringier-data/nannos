---
status: proposed (2026-09-22); implemented the same day in console-backend and the console
  (migration 105, `skill_activations.mode`), pending review
---

# A skill used by another agent is referenced, pinned or following, never copied

When a sub-agent activates a registry skill it does not own — a skill another
sub-agent published (ADR-0006) or a standalone import — its config version
references the publisher's row by id and content hash. There is one row and one
content history. The referrer's activation carries a **mode**: **pinned** (the
default: the hash moves only when a writer of the referrer clicks update) or
**following** (every content change on the publisher's row writes a new
auto-approved version of the referrer, in the publisher's transaction).
Because referrers hold no copy, **publishing is a commitment**: deleting a
referenced row, or making it private, is refused with a conflict naming the
referrers until the last one detaches.

## Context

- ADR-0006 lets a host publish a sub-agent skill as `public` so "every user can
  read and activate it on other agents", but said nothing about what the
  activating agent holds. `resolve_imported_skills` branched on the *row's*
  scope: a sub-agent-scoped row resolved as always-latest with the update signal
  forced off, which is correct for the row's owner and wrong for anyone else.
- Commit `fa676b38` on `fix-skills-registry-authz` worked around that by copying
  the published skill into a row the activating agent owns. The copy restored
  pinning and cut every write path back to the publisher, but it also cut the
  read path: a copy has no provenance, so "update available" can never be shown
  for it, and the two agents drift silently.
- The motivating scenario is the opposite of drift: the embedded cockpit agent
  is the authority for a knowledge-base skill, and a parallel knowledge-base-only
  agent must stay aligned with it revision for revision.

## Decision

1. **Reference, not copy.** The version stores `{registry_id, content_hash}` as
   it does for standalone imports today. Resolution branches on *ownership*
   (`skill_registry.sub_agent_id == this agent`), not on the row's scope: the
   owner sees always-latest, a referrer is pinned by hash and gets the existing
   update signal, diff and update action.
   _Amended by ADR-0013 (2026-09-25): the owner is pinned by hash too. Ownership
   now decides only what is reported around the skill (visibility, no mode), not
   which content is served; an own-skill edit outside a config save writes the
   owner's version._
2. **Two modes on the activation row, not in the version.** `skill_activations`
   gains `mode` (`pinned` default, `following`) plus a nullable last-bump error
   and timestamp. Mode is a relationship between agents; reverting the referrer
   to an older version must not detach it. A reference that reached the config
   through a plain config save has no activation row and is pinned by
   construction. Personal and group activations are pinned only.
3. **Following bumps, it does not resolve live.** At the single point where a
   registry row's content changes (`_save_version_snapshot` for an existing row),
   every following referrer gets one new version built from its *approved
   default* — never a pending draft, as `publish_managed_version` does for
   bindings — carrying the new hash and nothing else, approved on the spot and
   **signed by the seeded `system` user**. Neither the skill's author (who may
   have no access to the referrer) nor the user who once chose following (who
   consented, but did not act now) performed the change; the platform did,
   because a follow relationship exists. The change summary carries the
   provenance: skill, old and new hash, who edited it and on which publisher.
   Each bump runs in its own savepoint:
   a failure is recorded on the activation and skipped, the publisher's write
   commits, and the referrer stays on its previous hash showing "update
   available". Soft-deleted referrers are skipped silently. An embed-bound agent
   cannot be a referrer: its skill list is the host's and the sync prunes the rest.
4. **The referrer's writer chooses, alone.** Following is offered for any row the
   referrer does not own; no publisher consent, no provenance restriction. It is
   the referrer that hands part of its config to a third party, so it is the
   referrer that decides. The MCP tool description tells the agent to pick
   following only when the user asked for it.
5. **Re-activating with the other mode is the switch**; pinned→following bumps
   at once if behind. Deactivating detaches. No separate endpoint.
6. **Withdrawal is refused while referenced.** Delete and visibility→private on a
   referenced row return a conflict listing the referring agents, in the console
   and the MCP tools alike. Today deletion cascades onto activation rows and
   leaves a dead UUID in the referrer's config; the prune path already refuses
   that case with "a decision for a human, not a side effect of a sync".

7. **The console's mode control is the badge, not the save.** A pinned or
   following badge on the referrer's skill row toggles the mode through the
   activations endpoint (sub-agent scope). The edit-mode registry picker may
   offer "follow" as sugar: the front end saves the draft first, then makes the
   same activations call for the skills so marked. Mode never travels in a
   config-save payload, and following takes effect only once a version holding
   the skill is the approved default. A switch to following that has to catch
   up is the switching user's own action and is signed by them; the automatic
   bumps that follow are signed by `system` (decision 3).

## Considered options

- **Copy on activation** (`fa676b38`). Immune to withdrawal, but blind to updates
  and unable to satisfy the alignment scenario. Rejected; the commit is reverted.
- **Live reference** (resolve reads the publisher's current row at run time).
  Satisfies alignment but makes the referrer's approved version mutable: revert
  cannot restore content, and the referrer's history stops being an audit trail.
  Auto-bump gives the same alignment and keeps every version invariant; the only
  extra cost is one referrer version per publisher revision.
- **Convert referrers to copies on withdrawal.** Keeps publishers free but
  silently changes the nature of someone else's skill. Rejected for the refusal.
- **Follow only host-published rows, or publisher opt-in.** Rejected: the party
  taking the risk is the referrer, and a provenance check would be forgotten the
  first time a new source type appears.

## Consequences

- The referrer's owner gives up review of followed content. That is the feature.
- Hash-only bumps accumulate in referrers' histories; the change summary names
  the publisher and the new hash so the history reads as "followed X".
- A bump moves the referrer's `current_version` past any pending draft, exactly
  as a well-known sync does (`publish_managed_version`). The draft survives as a
  version and can still be reviewed; it is no longer "current". Following and
  a long-lived draft on the same agent do not mix well, and that is accepted.
- `upsert_locked` in `SkillActivationService` had no callers and would have wiped
  the mode column if revived; it is deleted.
- A host sync that flips a referenced public skill to `private` keeps it public
  and logs a warning, mirroring `prune_mirrored_skills`: a decision for a human,
  not a side effect of a sync.
- ADR-0006's "public" paragraph gains a pointer here. Its statement that the
  activating agent may "read and activate" stands; what it holds is defined here.
- Follow-ups, not in scope: a notification when a bump fails, and a UI hint on
  the referrer explaining why a following skill is behind.
- ADR-0012 adds `inline` on the `SkillRef`. It is a config-version property and
  independent of mode: a bump carries the new hash and keeps the existing `inline`
  value.
