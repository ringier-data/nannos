"""Tests for CEL watch conditions: one expression that extracts and gates.

The gate is derived from the result's type — a boolean gates directly, anything else
gates on non-empty — so extraction and gating can never disagree. Time-relative
conditions are the reason this exists: `now` is a variable, so "starts within the
hour" is decided by date math instead of a model guessing what time it is.
"""

from datetime import datetime, timezone

import pytest

from console_backend.services.cel_condition import (
    MAX_CEL_EXPR_LENGTH,
    MAX_CEL_PAYLOAD_BYTES,
    _CEL_EXECUTOR,
    CelEvaluationError,
    CelSyntaxError,
    evaluate_cel,
    to_python,
    validate_cel_expression,
)

NOW = datetime.fromisoformat("2026-08-24T11:30:00+02:00")

CALENDAR = {
    "events": [
        {
            "start": {"dateTime": "2026-08-24T12:00:00+02:00"},
            "attendees": [{"email": "bob@x.external.com"}],
        },
        {
            "start": {"dateTime": "2026-08-24T18:00:00+02:00"},
            "attendees": [{"email": "sam@company.com"}],
        },
    ]
}

WITHIN_THE_HOUR = (
    "result.events.filter(e, has(e.start.dateTime) "
    "&& timestamp(e.start.dateTime) > now "
    "&& timestamp(e.start.dateTime) - now < duration('1h'))"
)


class TestValidate:
    def test_a_valid_expression_passes(self):
        validate_cel_expression(WITHIN_THE_HOUR)

    def test_none_and_empty_are_allowed(self):
        validate_cel_expression(None)
        validate_cel_expression("")

    def test_a_parse_error_is_a_syntax_error(self):
        with pytest.raises(CelSyntaxError):
            validate_cel_expression("result.events.filter(")

    def test_an_oversized_expression_is_refused(self):
        with pytest.raises(CelSyntaxError):
            validate_cel_expression("result" + " || true" * (MAX_CEL_EXPR_LENGTH // 8))


class TestGateFromType:
    @pytest.mark.asyncio
    async def test_a_boolean_result_is_the_gate(self):
        cel = await evaluate_cel("size(result.events) > 1", result=CALENDAR, now=NOW)
        assert cel.gate is True
        assert cel.is_boolean is True
        assert cel.value is True

    @pytest.mark.asyncio
    async def test_a_non_empty_list_gates_true_and_is_the_evidence(self):
        cel = await evaluate_cel(WITHIN_THE_HOUR, result=CALENDAR, now=NOW)
        assert cel.gate is True
        assert cel.is_boolean is False
        # The extraction is the filtered events themselves — the noon meeting only.
        assert len(cel.value) == 1
        assert cel.value[0]["attendees"][0]["email"] == "bob@x.external.com"

    @pytest.mark.asyncio
    async def test_an_empty_list_gates_false(self):
        late = datetime.fromisoformat("2026-08-24T20:00:00+02:00")
        cel = await evaluate_cel(WITHIN_THE_HOUR, result=CALENDAR, now=late)
        assert cel.gate is False
        assert cel.value == []

    @pytest.mark.asyncio
    async def test_time_window_excludes_meetings_too_far_ahead(self):
        # 11:30 → only the 12:00 meeting is within the hour; at 09:00 nothing is.
        morning = datetime.fromisoformat("2026-08-24T09:00:00+02:00")
        cel = await evaluate_cel(WITHIN_THE_HOUR, result=CALENDAR, now=morning)
        assert cel.gate is False


class TestPrev:
    @pytest.mark.asyncio
    async def test_change_detection_against_the_previous_result(self):
        same = await evaluate_cel("result != prev", result=CALENDAR, now=NOW, prev=CALENDAR)
        changed = await evaluate_cel("result != prev", result=CALENDAR, now=NOW, prev={"events": []})
        assert same.gate is False
        assert changed.gate is True

    @pytest.mark.asyncio
    async def test_prev_is_null_on_the_first_run(self):
        cel = await evaluate_cel("prev == null", result=CALENDAR, now=NOW, prev=None)
        assert cel.gate is True


class TestErrors:
    @pytest.mark.asyncio
    async def test_a_missing_field_is_an_evaluation_error_not_false(self):
        with pytest.raises(CelEvaluationError):
            await evaluate_cel("result.nope.x", result=CALENDAR, now=NOW)

    @pytest.mark.asyncio
    async def test_a_parse_error_surfaces_as_syntax(self):
        with pytest.raises(CelSyntaxError):
            await evaluate_cel("result.events.filter(", result=CALENDAR, now=NOW)

    @pytest.mark.asyncio
    async def test_a_naive_now_is_tolerated(self):
        # Defensive: the evaluator always passes an aware datetime, but a naive one
        # must not crash timestamp arithmetic.
        cel = await evaluate_cel(
            "size(result.events)", result=CALENDAR, now=datetime(2026, 8, 24, 9, 30)
        )
        assert cel.value == 2


class TestArgsExpressions:
    """Dynamic check-tool arguments: CEL over `now`/`prev` returning a map.

    This is how a rolling report window reaches a tool argument without a literal
    date being stored on the job — the date is computed fresh on every run.
    """

    @pytest.mark.asyncio
    async def test_a_rolling_window_resolves_against_now(self):
        from console_backend.services.cel_condition import evaluate_arg_exprs

        args = await evaluate_arg_exprs(
            {
                "start_date": "strftime(now - duration('168h'), '%Y-%m-%d')",
                "end_date": "strftime(now, '%Y-%m-%d')",
            },
            now=NOW,
        )
        assert args == {"start_date": "2026-08-17", "end_date": "2026-08-24"}

    @pytest.mark.asyncio
    async def test_string_of_now_renders_iso(self):
        from console_backend.services.cel_condition import evaluate_arg_exprs

        args = await evaluate_arg_exprs({"timeMin": "string(now)"}, now=NOW)
        assert args["timeMin"].startswith("2026-08-24T")

    @pytest.mark.asyncio
    async def test_errors_name_the_argument_they_came_from(self):
        from console_backend.services.cel_condition import evaluate_arg_exprs

        with pytest.raises(CelSyntaxError, match="'end_date'"):
            await evaluate_arg_exprs(
                {"start_date": "string(now)", "end_date": "strftime(now,"}, now=NOW
            )

    @pytest.mark.asyncio
    async def test_result_is_not_available_before_the_tool_runs(self):
        from console_backend.services.cel_condition import evaluate_arg_exprs

        with pytest.raises(CelEvaluationError):
            await evaluate_arg_exprs({"x": "string(result.a)"}, now=NOW)

    @pytest.mark.asyncio
    async def test_prev_is_available_for_cursors(self):
        from console_backend.services.cel_condition import evaluate_arg_exprs

        args = await evaluate_arg_exprs(
            {"since_id": "has(prev.last_id) ? prev.last_id : 0"},
            now=NOW,
            prev={"last_id": 41},
        )
        assert args == {"since_id": 41}


class TestToPython:
    @pytest.mark.asyncio
    async def test_results_are_plain_json_types(self):
        import json

        cel = await evaluate_cel(
            "{'n': size(result.events), 'ok': true, 'names': ['a'], 'ratio': 0.5}",
            result=CALENDAR,
            now=NOW,
        )
        # BoolType subclasses int — without conversion json would render it as 1.
        assert json.dumps(cel.value) == '{"n": 2, "ok": true, "names": ["a"], "ratio": 0.5}'

    def test_none_passes_through(self):
        assert to_python(None) is None


class TestModelValidation:
    def test_create_refuses_a_watch_with_no_condition(self):
        from console_backend.models.scheduled_job import JobType, ScheduledJobCreate, ScheduleKind

        with pytest.raises(ValueError, match="require a condition"):
            ScheduledJobCreate(
                name="Watch nothing",
                job_type=JobType.WATCH,
                schedule_kind=ScheduleKind.INTERVAL,
                interval_seconds=300,
                check_tool="tool",
            )

    def test_create_accepts_cel_with_llm_condition(self):
        from console_backend.models.scheduled_job import JobType, ScheduledJobCreate, ScheduleKind

        job = ScheduledJobCreate(
            name="Gate then judge",
            job_type=JobType.WATCH,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=300,
            check_tool="tool",
            cel_expr=WITHIN_THE_HOUR,
            llm_condition="an attendee looks external",
        )
        assert job.cel_expr == WITHIN_THE_HOUR

    def test_create_refuses_an_uncompilable_cel_expr(self):
        from console_backend.models.scheduled_job import JobType, ScheduledJobCreate, ScheduleKind

        with pytest.raises(ValueError, match="CEL"):
            ScheduledJobCreate(
                name="Broken watch",
                job_type=JobType.WATCH,
                schedule_kind=ScheduleKind.INTERVAL,
                interval_seconds=300,
                check_tool="tool",
                cel_expr="result.filter(",
            )

    def test_update_refuses_an_uncompilable_cel_expr(self):
        from console_backend.models.scheduled_job import ScheduledJobUpdate

        with pytest.raises(ValueError, match="CEL"):
            ScheduledJobUpdate(cel_expr="&&")


class TestResourceCeilings:
    """The limits that actually bound evaluation, since the timeout cannot.

    `asyncio.wait_for` cancels the waiter, not the interpreter thread — Python cannot
    kill a thread — so an oversized expression or payload has to be refused before
    evaluation starts rather than interrupted once it is slow.
    """

    @pytest.mark.asyncio
    async def test_an_oversized_payload_is_refused_not_evaluated(self):
        # Cost is a function of the data as much as the expression, and a check tool's
        # response is not something the condition's author controls.
        huge = {"items": ["x" * 1024] * 2048}
        with pytest.raises(CelEvaluationError, match="the maximum an expression"):
            await evaluate_cel("size(result.items) > 0", huge, datetime.now(timezone.utc))

    @pytest.mark.asyncio
    async def test_an_oversized_prev_is_refused_too(self):
        huge = {"items": ["x" * 1024] * 2048}
        with pytest.raises(CelEvaluationError, match="`prev`"):
            await evaluate_cel("result != prev", {"a": 1}, datetime.now(timezone.utc), prev=huge)

    @pytest.mark.asyncio
    async def test_a_payload_just_under_the_ceiling_still_evaluates(self):
        ok = {"blob": "x" * (MAX_CEL_PAYLOAD_BYTES // 2)}
        cel = await evaluate_cel("size(result.blob) > 0", ok, datetime.now(timezone.utc))
        assert cel.gate is True

    def test_the_expression_cap_is_checked_before_the_parser(self):
        # A megabyte of nested parens must not reach the parser at all.
        with pytest.raises(CelSyntaxError, match="the maximum is"):
            validate_cel_expression("(" * (MAX_CEL_EXPR_LENGTH + 1))

    @pytest.mark.asyncio
    async def test_evaluation_does_not_use_the_shared_default_executor(self):
        # A burst of pathological expressions must degrade condition previews only, not
        # every other to_thread caller in the process.
        name = await evaluate_cel(
            "result.thread", {"thread": "x"}, datetime.now(timezone.utc)
        )
        assert name.value == "x"
        assert _CEL_EXECUTOR._thread_name_prefix == "cel"

    @pytest.mark.asyncio
    async def test_only_the_three_documented_names_are_bound(self):
        # Nothing else is reachable by name from an expression.
        with pytest.raises((CelEvaluationError, CelSyntaxError)):
            await evaluate_cel("__import__", {}, datetime.now(timezone.utc))
