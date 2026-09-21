"""102's replacements must match the prompt 075 seeded and 085 rewrote.

Same hazard as 085: a `replace()` whose search text does not match is a silent no-op in
SQL. The migration would report success and leave the task-scheduler holding the
sharing tools (`scheduler_share_job`, `scheduler_add_group_default_job`, …) with nothing
in its prompt saying they exist — and, worse, no rule for the one genuinely ambiguous
edit, a schedule change on a job several people subscribe to.

Asserted at the text level, applying 085's rewrite first so the chain is checked in the
order a real database sees it. No database needed, and it fails the moment any of the
three files is edited out of step with the others.
"""

import re
from pathlib import Path

DDL = Path(__file__).parent.parent / "sqlmigrations" / "ddl"
SEED = DDL / "075_reseed_task_scheduler_as_local_subagent.sql"
CEL = DDL / "085_reseed_task_scheduler_cel_conditions.sql"
SHARING = DDL / "102_task_scheduler_shared_jobs.sql"

#: The vocabulary the rewritten prompt has to carry, one item per decision of ADR-0010
#: that a conversation can actually hit.
TAUGHT = (
    "scheduler_share_job",
    "scheduler_subscribe_job",
    "scheduler_copy_job",
    "scheduler_add_group_default_job",
    "console_list_my_groups",
    # The ambiguous edit, and the answer to "why am I getting this?".
    "scope",
    "under ITS OWN",
)


def _sql_literals(text: str) -> list[str]:
    body = "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("--"))
    return [m.group(1).replace("''", "'") for m in re.finditer(r"'((?:[^']|'')*)'", body, re.DOTALL)]


def _replacement_pairs(path: Path, *, min_len: int = 40) -> list[tuple[str, str]]:
    """The (old, new) argument pairs of a migration whose replaces are all long text."""
    longs = [lit for lit in _sql_literals(path.read_text()) if len(lit) > min_len]
    return [(longs[i], longs[i + 1]) for i in range(0, len(longs) - len(longs) % 2, 2)]


def _set_clause(column: str) -> str:
    """The text of one ``<column> = … replace(...)`` SET clause of 102.

    Sliced rather than length-filtered: one of 102's search strings is the single
    short line ``<best_practices>``, so a filter wide enough to drop the jsonb tool
    array would drop that too and offset every pair after it.

    The two prompt columns are wrapped in a ``CASE WHEN … THEN cv.<column> ELSE`` guard
    that makes the up re-runnable after the down, so the slice starts at the assignment
    and skips to its first ``replace(``; the guard's own marker literal is short enough
    that ``_sql_literals`` pairing is unaffected only because we cut it off here.
    """
    sql = SHARING.read_text().split("-- rambler down", 1)[0]
    start = sql.index(f"{column} = ")
    rest = sql[start:]
    rest = rest[rest.index("replace(") :]
    end = min(
        (
            rest.index(marker)
            for marker in ("\n    ),\n", "\n    )\nFROM ", "\n    ) END,\n", "\n    ) END\nFROM ")
            if marker in rest
        ),
    )
    return rest[:end]


def _pairs_of(column: str) -> list[tuple[str, str]]:
    literals = _sql_literals(_set_clause(column))
    assert len(literals) % 2 == 0, f"{column}: odd number of replace() arguments"
    return [(literals[i], literals[i + 1]) for i in range(0, len(literals), 2)]


def _prompt_after_085() -> str:
    prompt = max(_sql_literals(SEED.read_text()), key=len)
    for old, new in _replacement_pairs(CEL):
        prompt = prompt.replace(old, new)
    return prompt


def test_every_replacement_finds_its_text():
    prompt = _prompt_after_085()
    pairs = _pairs_of("system_prompt")
    assert pairs, "102 defines no prompt replacements"
    for old, _ in pairs:
        assert old in prompt, f"102 searches for text the prompt never had: {old[:80]!r}"


def test_the_result_teaches_sharing():
    prompt = _prompt_after_085()
    for old, new in _pairs_of("system_prompt"):
        prompt = prompt.replace(old, new)

    for taught in TAUGHT:
        assert taught in prompt, f"the rewritten prompt never mentions {taught}"
    # Suspend must be told apart from "stop sending me this", which is the mistake that
    # would stop a job for a whole group when one person asked to leave it.
    assert "scheduler_suspend_job" in prompt and "unsubscribe" in prompt


def test_the_card_description_replacement_matches():
    pairs = _pairs_of("description")
    assert len(pairs) == 1
    description, replacement = pairs[0]
    seeded = [lit for lit in _sql_literals(SEED.read_text()) if description in lit]
    assert seeded, f"102's description edit searches for text 075 never seeded: {description!r}"
    assert "Share jobs with groups" in replacement


def test_every_tool_added_is_removed_again_by_the_down_migration():
    sql = SHARING.read_text()
    up, down = sql.split("-- rambler down", 1)
    added = set(re.findall(r'"(scheduler_[a-z_]+|console_[a-z_]+)"', up))
    removed = set(re.findall(r"'\"(scheduler_[a-z_]+|console_[a-z_]+)\"'::jsonb", down))
    assert added and added == removed, f"up/down disagree: {added ^ removed}"


def test_the_update_is_scoped_to_the_system_seed():
    sql = SHARING.read_text()
    assert sql.count("sa.owner_user_id = 'system'") == 2  # up and down
    assert sql.count("sa.name = 'task-scheduler'") == 2


def test_the_up_is_re_runnable_after_the_down():
    """A `rambler down` → `rambler up` cycle must not duplicate the prompt.

    The down deliberately keeps the prompt text and removes only `mcp_tools`, while every
    search string here is a substring of its own replacement — so without a guard the
    second up would insert the tool list, the `- Sharing:` line and the whole `<sharing>`
    block again. Both prompt columns are therefore wrapped in a CASE that leaves an
    already-taught prompt alone.
    """
    sql = SHARING.read_text().split("-- rambler down", 1)[0]
    for column, marker in (("system_prompt", "<sharing>"), ("description", "Share jobs with groups")):
        assignment = sql[sql.index(f"{column} = ") :]
        guard = assignment[: assignment.index("replace(")]
        assert "CASE WHEN" in guard and f"THEN cv.{column} ELSE" in guard, (
            f"{column} is applied unconditionally — a down/up cycle would duplicate it"
        )
        assert marker in guard, f"{column}'s guard does not test for the text it inserts"

    # And the guard actually holds: replaying the up twice over the post-085 prompt is
    # the same as replaying it once.
    prompt = _prompt_after_085()
    once = prompt
    for old, new in _pairs_of("system_prompt"):
        once = once.replace(old, new)
    assert "<sharing>" in once, "the first apply must teach sharing"

    twice = once
    for old, new in _pairs_of("system_prompt"):
        twice = twice.replace(old, new)
    # Unguarded, this is what would reach the database on a re-run …
    assert twice != once, "guard test is vacuous — the replaces are already idempotent"
    assert twice.count("<sharing>") == 2, "the duplication the guard exists to prevent"
    # … and the guard means the migration never evaluates that branch a second time.
