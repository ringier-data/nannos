"""085's replacements must actually match the prompt 075 seeded.

The migration rewrites the task-scheduler's stored prompt with three targeted
`replace()` calls. A `replace()` whose search text does not match is a silent no-op in
SQL: the migration would report success and leave the agent teaching an API that no
longer exists, which is how conversational watch creation broke in the first place. So
this asserts the match at the text level — no database needed, and it fails the moment
either file is edited out of step with the other.
"""

import re
from pathlib import Path

DDL = Path(__file__).parent.parent / "sqlmigrations" / "ddl"
SEED = DDL / "075_reseed_task_scheduler_as_local_subagent.sql"
RESEED = DDL / "085_reseed_task_scheduler_cel_conditions.sql"

#: Removed by 083; the prompt must not mention either again.
GONE = ("condition_expr", "expected_value")
#: The API the prompt has to teach instead.
TAUGHT = ("cel_expr", "llm_condition", "check_args_exprs")


def _sql_literals(text: str) -> list[str]:
    """The single-quoted literals of a migration, comment lines excluded.

    Comments carry apostrophes of their own ("agent's prompt"), which would otherwise
    open a literal and swallow half the file.
    """
    body = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("--"))
    return [m.group(1).replace("''", "'") for m in re.finditer(r"'((?:[^']|'')*)'", body, re.DOTALL)]


def _seeded_prompt() -> str:
    return max(_sql_literals(SEED.read_text()), key=len)


def _replacement_pairs() -> list[tuple[str, str]]:
    """The (old, new) argument pairs of 085's nested replace() calls."""
    longs = [lit for lit in _sql_literals(RESEED.read_text()) if len(lit) > 40]
    return [(longs[i], longs[i + 1]) for i in range(0, len(longs) - len(longs) % 2, 2)]


def test_every_replacement_finds_its_text():
    prompt = _seeded_prompt()
    pairs = _replacement_pairs()
    assert pairs, "085 defines no replacements"
    for old, _ in pairs:
        assert old in prompt, f"085 searches for text 075 never seeded: {old[:80]!r}"


def test_the_result_teaches_the_current_api():
    prompt = _seeded_prompt()
    for old, new in _replacement_pairs():
        prompt = prompt.replace(old, new)

    for removed in GONE:
        assert removed not in prompt, f"the rewritten prompt still teaches {removed}"
    for taught in TAUGHT:
        assert taught in prompt, f"the rewritten prompt never mentions {taught}"


def test_the_update_is_scoped_to_the_system_seed():
    sql = RESEED.read_text()
    assert "sa.owner_user_id = 'system'" in sql
    assert "sa.name = 'task-scheduler'" in sql
    # Re-running must not touch a prompt an operator has already moved off the old API.
    assert "LIKE '%condition_expr%'" in sql
