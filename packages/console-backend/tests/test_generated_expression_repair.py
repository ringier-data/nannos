"""Tests for correcting an uncompilable generated condition.

Three defences, tested here and in test_cel_condition.py: the prompt states the
language first, the generator checks its own output and retries with the compile
error, and the API refuses to store an expression that cannot compile.
"""

from unittest.mock import AsyncMock

import pytest

from console_backend.routers.scheduler_router import (
    _ARGS_RULES,
    _EXPRESSION_RULES,
    _GENERATE_CONDITION_RETRIES,
    _repair_cel,
)

BAD_CEL = "result.events.filter("
GOOD_CEL = "result.events.filter(e, has(e.attendees))"


class TestPromptRules:
    def test_conditions_are_asked_for_as_cel(self):
        assert "cel_expr" in _EXPRESSION_RULES
        assert "CEL" in _EXPRESSION_RULES

    def test_the_evaluation_environment_is_named(self):
        # The three variables are the whole contract; a model that does not know
        # `now` exists will put time windows into llm_condition.
        for variable in ("`result`", "`now`", "`prev`"):
            assert variable in _EXPRESSION_RULES

    def test_the_gate_rule_is_stated(self):
        # A boolean gates directly, anything else gates on non-empty — the model must
        # know returning the matching items is preferred.
        assert "boolean" in _EXPRESSION_RULES
        assert "non-empty" in _EXPRESSION_RULES

    def test_the_semantic_escape_hatch_is_spelled_out(self):
        # llm_condition is where genuinely semantic judgement goes, and the model must
        # be told the two compose (gate first, judge what passed).
        assert "llm_condition" in _EXPRESSION_RULES
        assert "COMPOSE" in _EXPRESSION_RULES

    def test_a_watch_needs_some_condition(self):
        assert "cel_expr, llm_condition, or both" in _EXPRESSION_RULES


class TestArgsRules:
    """check_args are static; a generated date in them watches the past forever.

    QA hit this twice: the draft generator put an absolute time window into a
    calendar tool's arguments. The rules must say args never carry dates, and that
    time filtering lives in the expression, where `now` moves with each run.
    """

    def test_args_are_declared_static(self):
        assert "STATIC" in _ARGS_RULES
        assert "unchanged on every run" in _ARGS_RULES

    def test_absolute_dates_are_forbidden_by_name(self):
        for term in ("absolute date", "timestamp", "fixed time window"):
            assert term in _ARGS_RULES

    def test_the_alternative_is_stated(self):
        # Forbidding without redirecting just moves the invented date elsewhere: a
        # moving argument goes in check_args_expr, resolved against `now` on each run.
        assert "check_args_exprs" in _ARGS_RULES
        assert "`now`" in _ARGS_RULES
        assert "strftime" in _ARGS_RULES

    def test_response_side_filtering_is_still_preferred(self):
        # Narrowing at the tool hides the evidence; filtering in cel_expr keeps it on
        # the run. The prompt must keep steering there when both would work.
        assert "cel_expr" in _ARGS_RULES

    def test_required_date_arguments_must_come_from_the_expression(self):
        # QA hit this: told not to invent dates, the model left REQUIRED date
        # arguments empty instead of supplying them as a rolling window.
        assert "REQUIRES a date/time argument" in _ARGS_RULES
        assert "MUST provide it through check_args_exprs" in _ARGS_RULES
        assert "never by leaving it empty" in _ARGS_RULES

    def test_map_keys_must_match_the_tool_schema(self):
        # The example's key names must not leak into tools that spell them
        # differently (date_from vs start_date).
        assert "exact argument names" in _ARGS_RULES


class TestCelRepair:
    @pytest.mark.asyncio
    async def test_a_compilable_expression_is_left_alone(self):
        generate = AsyncMock()
        result = await _repair_cel({"cel_expr": GOOD_CEL}, "p", generate, "q")
        assert result["cel_expr"] == GOOD_CEL
        generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_expression_needs_no_repair(self):
        generate = AsyncMock()
        assert await _repair_cel({"check_tool": "t"}, "p", generate, "q") == {"check_tool": "t"}
        generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_uncompilable_expression_is_retried_with_the_error(self):
        generate = AsyncMock(return_value={"cel_expr": GOOD_CEL})
        result = await _repair_cel({"cel_expr": BAD_CEL, "check_tool": "t"}, "P", generate, "q")

        assert result["cel_expr"] == GOOD_CEL
        assert result["check_tool"] == "t"  # the rest of the generation survives
        retry_prompt = generate.await_args.args[0]
        assert BAD_CEL in retry_prompt  # the model is told what it produced
        assert "does not compile" in retry_prompt

    @pytest.mark.asyncio
    async def test_a_second_failure_falls_back_to_a_judged_condition(self):
        # Always valid — so the worst case is a working (if pricier) job, not a broken one.
        generate = AsyncMock(return_value={"cel_expr": "still broken ("})
        result = await _repair_cel(
            {"cel_expr": BAD_CEL}, "P", generate, "tell me when an outsider is invited"
        )
        assert result["cel_expr"] is None
        assert result["llm_condition"] == "tell me when an outsider is invited"

    @pytest.mark.asyncio
    async def test_it_retries_as_often_as_the_condition_endpoint_does(self):
        # The two paths used to disagree — one retry here, two there — so an improvement
        # to one silently missed the other. Pinned so they cannot drift apart again.
        generate = AsyncMock(return_value={"cel_expr": "still broken ("})
        await _repair_cel({"cel_expr": BAD_CEL}, "P", generate, "q")
        assert generate.await_count == _GENERATE_CONDITION_RETRIES

    @pytest.mark.asyncio
    async def test_a_generated_llm_condition_is_preferred_over_the_raw_query(self):
        generate = AsyncMock(return_value={"llm_condition": "an external attendee is invited"})
        result = await _repair_cel({"cel_expr": BAD_CEL}, "P", generate, "raw query")
        assert result["cel_expr"] is None
        assert result["llm_condition"] == "an external attendee is invited"

    @pytest.mark.asyncio
    async def test_a_failing_retry_still_yields_a_usable_job(self):
        generate = AsyncMock(side_effect=RuntimeError("gateway down"))
        result = await _repair_cel({"cel_expr": BAD_CEL}, "P", generate, "q")
        assert result["cel_expr"] is None
        assert result["llm_condition"] == "q"
