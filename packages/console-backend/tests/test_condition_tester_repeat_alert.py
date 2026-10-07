"""The condition tester warns about re-alerting on the same items — only when it would happen.

A watch built by the assistant used `size(result.threads) > 0` on a repeating schedule: every
run alerted again on the same unread threads. The skill already said to compare against
`prev`; the tester now says so with the evidence, by evaluating the expression a second time
with `prev` bound to this very response (what the next run sees when nothing changed).
"""

import pytest

from console_backend.models.scheduled_job import ValidateConditionRequest
from console_backend.routers.scheduler_router import _validate_cel_condition

THREADS = {"threads": [{"id": "t1", "subject": "a"}, {"id": "t2", "subject": "b"}]}


async def _notes(expr: str, result: object) -> list[str]:
    response = await _validate_cel_condition(ValidateConditionRequest(result=result, cel_expr=expr))
    assert response.valid, response.error
    return response.notes or []


def _repeat(notes: list[str]) -> list[str]:
    return [n for n in notes if "on every run" in n]


@pytest.mark.asyncio
async def test_a_presence_check_on_identified_items_is_flagged_with_the_list_path():
    [note] = _repeat(await _notes("size(result.threads) > 0", THREADS))
    assert note.startswith("If nothing changes before the next run, this fires again")
    assert "the same 2 item(s)" in note
    assert "result.threads.filter(t, prev == null || !has(prev.threads)" in note


@pytest.mark.asyncio
async def test_a_condition_that_keeps_only_new_items_is_not_flagged():
    expr = "result.threads.filter(t, prev == null || !has(prev.threads) || !prev.threads.exists(p, p.id == t.id))"
    assert _repeat(await _notes(expr, THREADS)) == []


@pytest.mark.asyncio
async def test_reading_prev_without_de_duplicating_is_called_out():
    expr = "result.threads.filter(t, prev == null || size(prev.threads) >= 0)"
    [note] = _repeat(await _notes(expr, THREADS))
    assert note.startswith("It reads prev but still fires when the response has not changed")


@pytest.mark.asyncio
async def test_a_lasting_state_is_left_alone():
    # "Alert while the service is down" re-alerting each run can be the point.
    assert _repeat(await _notes("result.status == 'down'", {"status": "down"})) == []


@pytest.mark.asyncio
async def test_a_gate_that_is_not_met_says_nothing_about_repeats():
    assert _repeat(await _notes("size(result.threads) > 5", THREADS)) == []
