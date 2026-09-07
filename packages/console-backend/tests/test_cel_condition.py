"""Tests for CEL watch conditions: one expression that extracts and gates.

The gate is derived from the result's type — a boolean gates directly, anything else
gates on non-empty — so extraction and gating can never disagree. Time-relative
conditions are the reason this exists: `now` is a variable, so "starts within the
hour" is decided by date math instead of a model guessing what time it is.
"""

from datetime import datetime, timezone

import pytest

from console_backend.services import cel_condition
from console_backend.services.cel_condition import (
    CEL_SYNTAX_HINT,
    CEL_EVAL_TIMEOUT_SECONDS,
    MAX_CEL_EXPR_LENGTH,
    MAX_CEL_PAYLOAD_BYTES,
    MAX_CEL_STEPS,
    _CEL_EXECUTOR,
    CelBudgetExceededError,
    CelDeadlineExceededError,
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


class TestStepBudget:
    """The step meter, which is the only ceiling that acts on work already running.

    The input caps refuse an oversized expression or payload before evaluation starts,
    but cost is not a function of size alone: a short expression over a small payload
    can still be quadratic. These tests pin the three things that make the meter work —
    it counts inside macros, it is not reset per item, and it cannot be swallowed.
    """

    QUADRATIC = "result.items.filter(i, result.items.exists(j, j == i))"

    @pytest.fixture
    def small_budget(self, monkeypatch):
        """Shrink the ceiling so a test trips it in milliseconds, not seconds."""
        monkeypatch.setattr(cel_condition, "MAX_CEL_STEPS", 2_000)

    @pytest.mark.asyncio
    async def test_a_quadratic_comprehension_is_stopped(self, small_budget):
        # Well under both input caps — ~11 KB of payload, a 60-character expression —
        # and still unbounded work, which is the case the meter exists for.
        payload = {"items": list(range(2000))}
        with pytest.raises(CelBudgetExceededError, match="expression nodes"):
            await evaluate_cel(self.QUADRATIC, payload, NOW)

    @pytest.mark.asyncio
    async def test_the_budget_is_not_reset_for_each_macro_item(self, small_budget):
        # celpy evaluates a macro body in a nested evaluator. If the meter did not
        # travel into it, or restarted per item, this would pass under any ceiling:
        # each item on its own costs almost nothing.
        payload = {"items": list(range(500))}
        with pytest.raises(CelBudgetExceededError):
            await evaluate_cel("result.items.map(i, i * 2)", payload, NOW)

    @pytest.mark.asyncio
    async def test_short_circuiting_cannot_swallow_the_budget_error(self, small_budget):
        # The reason CelBudgetExceededError is not a CELEvalError: celpy bottles those
        # up as values to implement `||`, so an expensive left operand would be quietly
        # discarded and the whole expression would return true.
        payload = {"items": list(range(2000))}
        with pytest.raises(CelBudgetExceededError):
            await evaluate_cel(f"({self.QUADRATIC}).size() > 0 || true", payload, NOW)

    @pytest.mark.asyncio
    async def test_a_filter_inside_a_macro_cannot_hide_from_the_meter(self, small_budget):
        payload = {"items": [{"tags": list(range(50))} for _ in range(50)]}
        with pytest.raises(CelBudgetExceededError):
            await evaluate_cel(
                "result.items.filter(i, i.tags.exists(t, t > 48))", payload, NOW
            )

    @pytest.mark.asyncio
    async def test_a_realistic_condition_stays_well_inside_the_budget(self):
        # Regression guard on the ceiling itself: a filter over a large tool response
        # is the shape conditions actually have, and must not be collateral damage.
        payload = {
            "events": [
                {"start": {"dateTime": "2026-08-24T12:00:00+02:00"}, "attendees": []}
                for _ in range(500)
            ]
        }
        cel = await evaluate_cel(WITHIN_THE_HOUR, payload, NOW)
        assert cel.gate is True
        assert len(cel.value) == 500
        assert cel_condition._ENV.runnable.last_steps < MAX_CEL_STEPS // 4

    @pytest.mark.asyncio
    async def test_the_budget_error_names_the_argument_it_came_from(self, small_budget):
        from console_backend.services.cel_condition import evaluate_arg_exprs

        with pytest.raises(CelBudgetExceededError, match="'window'"):
            await evaluate_arg_exprs(
                {"window": "[0].map(i, [1,2,3,4,5,6,7,8].map(j, [1,2,3,4,5,6,7,8]"
                 ".map(k, [1,2,3,4,5,6,7,8].map(l, l + k + j))))"},
                NOW,
            )

    @pytest.mark.asyncio
    async def test_the_interpreter_stops_itself_before_the_caller_times_out(self, monkeypatch):
        # The point of an in-interpreter deadline: `asyncio.wait_for` ends the wait but
        # leaves the thread running, so under load the caller used to see a bare timeout
        # while the work carried on unobserved behind it.
        monkeypatch.setattr(cel_condition, "CEL_EVAL_DEADLINE_SECONDS", 0.05)
        payload = {"items": list(range(2000))}
        with pytest.raises(CelDeadlineExceededError, match="was stopped"):
            await evaluate_cel(self.QUADRATIC, payload, NOW)

    @pytest.mark.asyncio
    async def test_the_deadline_leaves_a_normal_condition_alone(self):
        payload = {
            "events": [
                {"start": {"dateTime": "2026-08-24T12:00:00+02:00"}, "attendees": []}
                for _ in range(500)
            ]
        }
        assert (await evaluate_cel(WITHIN_THE_HOUR, payload, NOW)).gate is True

    def test_the_deadline_is_set_below_the_caller_timeout(self):
        # If these ever cross, the timeout wins and the specific error is unreachable.
        assert cel_condition.CEL_EVAL_DEADLINE_SECONDS < CEL_EVAL_TIMEOUT_SECONDS

    def test_the_meter_survives_into_macro_bodies(self):
        # The superclass hardcodes `Evaluator(...)` in sub_evaluator, so a missing
        # override would silently drop the meter at every map/filter/all/exists.
        from console_backend.services.cel_condition import MeteredEvaluator, _StepMeter

        meter = _StepMeter(limit=10, deadline=float("inf"))
        evaluator = MeteredEvaluator(ast=None, activation=None, meter=meter)
        nested = evaluator.sub_evaluator(ast=None)
        assert isinstance(nested, MeteredEvaluator)
        assert nested.meter is meter


class TestSyntaxHintTeachesWhatTheEngineSupports:
    """Every construct the hint teaches must actually work, on this engine.

    The hint is what a model writes conditions from, so an example that does not
    evaluate here is a condition that will fail on its first poll.
    """

    @pytest.mark.parametrize(
        "expr,payload,expected",
        [
            ("'code' in result && result['code'] == 200", {"code": 200}, True),
            ("'code' in result && result['code'] == 200", {"other": 1}, False),
            (
                "result.total != 0 && double(result.hits) / double(result.total) > 0.5",
                {"hits": 3, "total": 4},
                True,
            ),
            (
                "result.total != 0 && double(result.hits) / double(result.total) > 0.5",
                {"hits": 3, "total": 0},
                False,
            ),
            ("type(result.tags) == list && 'urgent' in result.tags", {"tags": ["urgent"]}, True),
            ("type(result.tags) == list && 'urgent' in result.tags", {"tags": "urgent"}, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_guard_examples_evaluate(self, expr, payload, expected):
        assert (await evaluate_cel(expr, payload, NOW)).gate is expected

    def test_the_hint_does_not_teach_optional_syntax(self):
        # celpy has no `.?`, `[?]` or orValue() — they are a CEL extension it does not
        # implement — so the hint must keep steering to has() and in.
        assert "orValue" not in CEL_SYNTAX_HINT
        assert "has()" in CEL_SYNTAX_HINT and " in " in CEL_SYNTAX_HINT

    def test_the_hint_advertises_only_registered_functions(self):
        from console_backend.services.cel_condition import _EXTENSION_FUNCTIONS

        assert "strftime" in _EXTENSION_FUNCTIONS
        # jsonpath() and eq_ci() stay registered for migrated conditions but must not
        # be taught: they are the only part of a stored condition a conformant CEL
        # engine would not understand.
        assert "jsonpath(" not in CEL_SYNTAX_HINT
        assert "eq_ci(" not in CEL_SYNTAX_HINT


class TestMigrationCompatibilityFunctions:
    """jsonpath() and eq_ci() are no longer advertised, so these tests are what keep them.

    Migration 083 rewrote every stored JSONPath condition onto these two functions on the
    promise that no verdict would change. That makes them load-bearing for jobs nobody
    will re-author — while CEL_SYNTAX_HINT deliberately no longer mentions them, because
    they are the only part of a stored condition a conformant CEL engine would not
    understand. Unadvertised plus untested is what gets deleted by a tidy-up, so the
    contracts the migration relied on are pinned here rather than left to the docstring.
    """

    NOW = datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_jsonpath_returns_null_for_no_match(self):
        cel = await evaluate_cel('jsonpath(result, "$.missing")', {"a": 1}, self.NOW)
        assert cel.value is None
        assert cel.gate is False  # nothing extracted is nothing to trigger on

    @pytest.mark.asyncio
    async def test_jsonpath_returns_the_value_itself_for_one_match(self):
        # Not a one-element list: the old extractor unwrapped, and a condition comparing
        # against a scalar would break if this started wrapping.
        cel = await evaluate_cel('jsonpath(result, "$.a.b")', {"a": {"b": "x"}}, self.NOW)
        assert cel.value == "x"

    @pytest.mark.asyncio
    async def test_jsonpath_returns_a_list_for_several_matches(self):
        cel = await evaluate_cel(
            'jsonpath(result, "$.items[*].n")', {"items": [{"n": 1}, {"n": 2}]}, self.NOW
        )
        assert cel.value == [1, 2]

    @pytest.mark.asyncio
    async def test_jsonpath_does_what_native_cel_cannot(self):
        # The one capability with no native spelling: recursive descent to unknown depth.
        cel = await evaluate_cel('jsonpath(result, "$..n")', {"x": {"y": {"n": 7}}}, self.NOW)
        assert cel.value == 7

    @pytest.mark.asyncio
    async def test_eq_ci_compares_case_insensitively(self):
        cel = await evaluate_cel('eq_ci(result.s, "failed")', {"s": "FAILED"}, self.NOW)
        assert cel.value is True
        assert cel.is_boolean  # a boolean result gates directly

    @pytest.mark.asyncio
    async def test_eq_ci_compares_both_sides_as_text(self):
        # The old comparison stringified, so a numeric payload matched a string expected
        # value. Jobs migrated on that behaviour.
        cel = await evaluate_cel('eq_ci(result.code, "200")', {"code": 200}, self.NOW)
        assert cel.value is True

    @pytest.mark.asyncio
    async def test_eq_ci_needs_no_regex_escaping(self):
        # Why it stays registered rather than being dropped for matches("(?i)…"): the
        # standard spelling would read these metacharacters as a pattern.
        cel = await evaluate_cel('eq_ci(result.v, "V1.2+BUILD")', {"v": "v1.2+build"}, self.NOW)
        assert cel.value is True

    @pytest.mark.asyncio
    async def test_the_advertised_case_insensitive_spelling_works(self):
        # What CEL_SYNTAX_HINT teaches instead, and the reason it can: matches is core CEL.
        cel = await evaluate_cel('result.s.matches("(?i)^failed$")', {"s": "FAILED"}, self.NOW)
        assert cel.value is True

    @pytest.mark.asyncio
    async def test_the_strings_extension_is_not_available(self):
        # lowerAscii() would be the obvious spelling; celpy does not register it, which is
        # why the hint teaches matches() and why eq_ci had no native equivalent.
        with pytest.raises((CelEvaluationError, CelSyntaxError)):
            await evaluate_cel('result.s.lowerAscii() == "failed"', {"s": "FAILED"}, self.NOW)
