"""Tests for evaluating a watch job's condition in the scheduler.

A condition is a CEL expression (extracts the evidence and gates the trigger in one),
a model judgement over the response, or both stacked: the gate runs first and the
model judges only what the expression returned.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from console_backend.models.scheduled_job import JobType, ScheduledJob, ScheduleKind
from console_backend.models.scheduled_job import ConditionEvaluation
from console_backend.services.mcp_tool_client import GatewayError, ToolCallResult
from console_backend.services.watch_evaluator import WatchEvaluator

NOW = datetime.now(timezone.utc)


def _job(**overrides) -> ScheduledJob:
    fields = {
        "id": 1,
        "user_id": "u1",
        "name": "Sync watch",
        "job_type": JobType.WATCH,
        "schedule_kind": ScheduleKind.INTERVAL,
        "interval_seconds": 300,
        "next_run_at": NOW + timedelta(minutes=5),
        "check_tool": "naonous_get_campaign",
        "check_args": {"campaign_id": "4821"},
        "cel_expr": "result.items.filter(i, i.status == 'FAILED')",
        "enabled": True,
        "max_failures": 3,
        "consecutive_failures": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return ScheduledJob(**fields)


def _gateway(monkeypatch, result: dict, *, is_error: bool = False):
    monkeypatch.setattr(
        "console_backend.services.watch_evaluator.token_for",
        AsyncMock(return_value="gw-token"),
    )
    monkeypatch.setattr(
        "console_backend.services.watch_evaluator.call_tool",
        AsyncMock(return_value=ToolCallResult(result=result, elapsed_ms=12, is_error=is_error)),
    )


class TestCanEvaluate:
    def test_gateway_tools_are_evaluated_here(self):
        assert WatchEvaluator.can_evaluate(_job()) is True

    def test_console_served_tools_are_evaluated_here_too(self):
        assert WatchEvaluator.can_evaluate(_job(check_tool="console_list_mcp_tools")) is True

    def test_task_jobs_are_not_watches(self):
        assert WatchEvaluator.can_evaluate(_job(job_type=JobType.TASK, sub_agent_id=1)) is False

    def test_a_watch_without_a_tool_is_declined(self):
        assert WatchEvaluator.can_evaluate(_job(check_tool=None)) is False


class TestAMissingCredentialIsAnAskNotAFailure:
    """The check tool answering ``need-credentials`` parks the run (ADR-0009).

    Only the agent path used to detect this: a check made here fell into the generic
    tool-error branch, the run failed with the gateway's raw payload as its message, and
    a few polls later the job auto-paused on a condition no retry can fix.
    """

    PAYLOAD = {
        "errorCode": "need-credentials",
        "authorizeUrl": "https://gateway.example/oauth/gt_abc/begin",
        "message": "This tool requires secondary authorization.",
    }

    @pytest.mark.asyncio
    async def test_need_credentials_is_returned_as_an_ask(self, monkeypatch):
        _gateway(monkeypatch, dict(self.PAYLOAD), is_error=True)
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "token")

        assert outcome.error is None, "a missing credential must not count toward max_failures"
        assert outcome.condition_met is False
        assert outcome.evaluation is None
        requirement = outcome.auth_ask["auth_requirement"]
        assert requirement["resource"] == "naonous_get_campaign"
        assert requirement["auth_methods"][0]["auth_url"] == self.PAYLOAD["authorizeUrl"]
        assert requirement["auth_methods"][0]["description"] == self.PAYLOAD["message"]

    @pytest.mark.asyncio
    async def test_the_raw_payload_stays_off_the_run(self, monkeypatch):
        # The authorize URL belongs in the ask the console renders, not in an error banner.
        _gateway(monkeypatch, dict(self.PAYLOAD), is_error=True)
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "token")
        assert outcome.check_result is None

    @pytest.mark.asyncio
    async def test_detected_even_when_the_tool_did_not_flag_an_error(self, monkeypatch):
        # Some servers return the refusal as an ordinary result; the field is the truth.
        _gateway(monkeypatch, dict(self.PAYLOAD), is_error=False)
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "token")
        assert outcome.auth_ask is not None

    @pytest.mark.asyncio
    async def test_business_data_mentioning_credentials_is_not_an_ask(self, monkeypatch):
        _gateway(monkeypatch, {"status": "OK", "note": "errorCode need-credentials appears in prose"})
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(cel_expr="result.status == 'FAILED'"), "token")
        assert outcome.auth_ask is None
        assert outcome.error is None

    @pytest.mark.asyncio
    async def test_a_refusal_wrapped_in_prose_is_still_an_ask(self, monkeypatch):
        # Several content blocks, or JSON inside a sentence, fold under "output".
        wrapped = "Tool call failed: " + json.dumps(self.PAYLOAD) + ". Please try again."
        _gateway(monkeypatch, {"output": wrapped}, is_error=True)
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "token")
        method = outcome.auth_ask["auth_requirement"]["auth_methods"][0]
        assert method["auth_url"] == self.PAYLOAD["authorizeUrl"]
        assert method["description"] == self.PAYLOAD["message"]

    @pytest.mark.asyncio
    async def test_a_payload_without_a_url_is_still_an_ask(self, monkeypatch):
        _gateway(monkeypatch, {"errorCode": "need-credentials"}, is_error=True)
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "token")
        method = outcome.auth_ask["auth_requirement"]["auth_methods"][0]
        assert "auth_url" not in method
        assert "naonous_get_campaign" in method["description"]


class TestFailuresAreNotQuietOutcomes:
    """A watch that cannot see its subject must not look like one whose answer is no."""

    @pytest.mark.asyncio
    async def test_an_unreachable_gateway_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            "console_backend.services.watch_evaluator.token_for",
            AsyncMock(return_value="gw-token"),
        )
        monkeypatch.setattr(
            "console_backend.services.watch_evaluator.call_tool",
            AsyncMock(side_effect=GatewayError("Cannot reach Gatana MCP gateway: ConnectError")),
        )
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "tok")
        assert outcome.condition_met is False
        assert "Cannot reach" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_a_refused_token_exchange_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            "console_backend.services.watch_evaluator.token_for",
            AsyncMock(side_effect=ValueError("Failed to exchange token")),
        )
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "tok")
        assert "exchange" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_a_tool_reporting_its_own_failure_is_an_error(self, monkeypatch):
        _gateway(monkeypatch, {"output": "campaign 4821 not found"}, is_error=True)
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "tok")
        assert outcome.condition_met is False
        assert "reported an error" in (outcome.error or "")
        assert outcome.check_result == {"output": "campaign 4821 not found"}

    @pytest.mark.asyncio
    async def test_a_conditionless_watch_is_an_error_not_a_silent_no(self, monkeypatch):
        # Validation refuses to store one; a row that still has no condition predates
        # the CEL migration and must fail loudly rather than poll forever.
        _gateway(monkeypatch, {"a": 1})
        outcome = await WatchEvaluator().evaluate(
            AsyncMock(), _job(cel_expr=None, llm_condition=None), "tok"
        )
        assert outcome.condition_met is False
        assert "no condition" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_a_check_that_never_ran_records_no_evaluation(self, monkeypatch):
        # Nothing was decided, so there is nothing to explain — distinct from a decision
        # that came out false.
        monkeypatch.setattr(
            "console_backend.services.watch_evaluator.token_for",
            AsyncMock(side_effect=ValueError("exchange refused")),
        )
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "tok")
        assert outcome.evaluation is None


class TestCelConditions:
    """One CEL expression extracts the evidence and gates the trigger.

    A boolean result gates directly; anything else gates on non-empty — so what the
    run records as `extracted` is exactly what decided it, and (with an llm_condition
    stacked on top) exactly what the model is given to judge.
    """

    @pytest.mark.asyncio
    async def test_a_non_empty_extraction_triggers_and_is_the_evidence(self, monkeypatch):
        _gateway(monkeypatch, {"items": [{"status": "FAILED", "id": 7}, {"status": "OK", "id": 8}]})
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "tok")
        assert outcome.condition_met is True
        assert outcome.evaluation.mode == "cel"
        assert outcome.evaluation.gate_met is True
        assert outcome.evaluation.extracted == [{"status": "FAILED", "id": 7}]

    @pytest.mark.asyncio
    async def test_the_extraction_is_the_evidence_a_triggered_watch_hands_on(self, monkeypatch):
        # The expression is gate and filter in one: what it matched is what the agent
        # acts on and what the notification is written from, not the whole response.
        _gateway(monkeypatch, {"items": [{"status": "FAILED", "id": 7}, {"status": "OK", "id": 8}]})
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "tok")
        assert outcome.evidence == [{"status": "FAILED", "id": 7}]

    @pytest.mark.asyncio
    async def test_a_boolean_gate_narrows_nothing(self, monkeypatch):
        _gateway(monkeypatch, {"items": [1, 2, 3]})
        outcome = await WatchEvaluator().evaluate(
            AsyncMock(), _job(cel_expr="size(result.items) > 2"), "tok"
        )
        assert outcome.evidence is None

    @pytest.mark.asyncio
    async def test_a_scalar_extraction_narrows_nothing_worth_acting_on(self, monkeypatch):
        # A bare "FAILED" (the shape every migrated jsonpath watch returns) carries none
        # of the ids an agent needs, so the whole response goes as before.
        _gateway(monkeypatch, {"status": "FAILED", "incident": 41})
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(cel_expr="result.status"), "tok")
        assert outcome.condition_met is True
        assert outcome.evidence is None

    @pytest.mark.asyncio
    async def test_a_judge_on_top_does_not_widen_the_evidence(self, monkeypatch):
        # The judge is shown the extraction and the full response to decide; what the
        # run hands on is still what the expression matched.
        _gateway(monkeypatch, {"items": [{"status": "FAILED", "id": 7}, {"status": "OK", "id": 8}]})
        monkeypatch.setattr(
            "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
            AsyncMock(return_value={"chat:low": "m"}),
        )
        monkeypatch.setattr(
            "console_backend.services.llm_gateway.gateway_chat",
            AsyncMock(return_value='{"condition_met": true, "reasoning": "disk full"}'),
        )
        outcome = await WatchEvaluator().evaluate(
            AsyncMock(), _job(llm_condition="the failure looks like an outage"), "tok"
        )
        assert outcome.condition_met is True
        assert outcome.evaluation.mode == "cel+judge"
        assert outcome.evidence == [{"status": "FAILED", "id": 7}]

    @pytest.mark.asyncio
    async def test_an_empty_extraction_is_a_quiet_poll(self, monkeypatch):
        _gateway(monkeypatch, {"items": [{"status": "OK"}]})
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "tok")
        assert outcome.condition_met is False
        assert outcome.error is None
        assert outcome.evaluation == ConditionEvaluation(
            met=False, mode="cel", gate_met=False, extracted=[]
        )

    @pytest.mark.asyncio
    async def test_a_boolean_expression_gates_directly(self, monkeypatch):
        _gateway(monkeypatch, {"items": [1, 2, 3]})
        outcome = await WatchEvaluator().evaluate(
            AsyncMock(), _job(cel_expr="size(result.items) > 2"), "tok"
        )
        assert outcome.condition_met is True
        assert outcome.evaluation.extracted is True

    @pytest.mark.asyncio
    async def test_time_relative_conditions_use_the_real_clock(self, monkeypatch):
        # The reason CEL exists here: "starts within the hour" decided by date math
        # against an injected `now`, not by a model guessing the date from the payload.
        soon = (NOW + timedelta(minutes=30)).isoformat()
        far = (NOW + timedelta(hours=6)).isoformat()
        _gateway(monkeypatch, {"events": [{"start": soon}, {"start": far}]})
        outcome = await WatchEvaluator().evaluate(
            AsyncMock(),
            _job(
                cel_expr=(
                    "result.events.filter(e, timestamp(e.start) > now "
                    "&& timestamp(e.start) - now < duration('1h'))"
                )
            ),
            "tok",
        )
        assert outcome.condition_met is True
        assert outcome.evaluation.extracted == [{"start": soon}]

    @pytest.mark.asyncio
    async def test_prev_is_the_last_check_result(self, monkeypatch):
        _gateway(monkeypatch, {"items": [1]})
        unchanged = await WatchEvaluator().evaluate(
            AsyncMock(), _job(cel_expr="result != prev", last_check_result={"items": [1]}), "tok"
        )
        changed = await WatchEvaluator().evaluate(
            AsyncMock(), _job(cel_expr="result != prev", last_check_result={"items": []}), "tok"
        )
        assert unchanged.condition_met is False
        assert changed.condition_met is True

    @pytest.mark.asyncio
    async def test_a_jsonpath_extraction_keeps_working_through_cel(self, monkeypatch):
        # Conditions migrated from the old JSONPath rows go through the jsonpath()
        # extension; their verdicts must not change out from under the jobs.
        _gateway(monkeypatch, {"campaign": {"sync": {"status": "FAILED"}}})
        outcome = await WatchEvaluator().evaluate(
            AsyncMock(),
            _job(cel_expr='eq_ci(jsonpath(result, "$.campaign.sync.status"), "failed")'),
            "tok",
        )
        assert outcome.condition_met is True

    @pytest.mark.asyncio
    async def test_an_evaluation_error_fails_the_run_not_the_condition(self, monkeypatch):
        # A condition that cannot see its subject must not read as "not met".
        _gateway(monkeypatch, {"different": "shape"})
        outcome = await WatchEvaluator().evaluate(AsyncMock(), _job(), "tok")
        assert outcome.condition_met is False
        assert "failed" in (outcome.error or "")
        assert outcome.check_result == {"different": "shape"}


class TestDynamicCheckArgs:
    """check_args_expr resolves before the tool call: rolling windows, fresh each poll."""

    @pytest.mark.asyncio
    async def test_resolved_args_are_merged_over_the_static_ones(self, monkeypatch):
        _gateway(monkeypatch, {"items": [{"status": "FAILED"}]})
        call_tool = AsyncMock(
            return_value=ToolCallResult(result={"items": []}, elapsed_ms=5, is_error=False)
        )
        monkeypatch.setattr("console_backend.services.watch_evaluator.call_tool", call_tool)
        await WatchEvaluator().evaluate(
            AsyncMock(),
            _job(
                check_args={"report": "sales", "date_from": "stale"},
                check_args_exprs={"date_from": "strftime(now, '%Y-%m-%d')"},
            ),
            "tok",
        )
        args = call_tool.await_args.args[2]
        assert args["report"] == "sales"  # static survives
        # The expression wins per key: the stale stored date is replaced by today's.
        assert args["date_from"] != "stale"
        assert len(args["date_from"]) == 10

    @pytest.mark.asyncio
    async def test_a_failing_args_expr_fails_the_run_without_calling_the_tool(self, monkeypatch):
        # Calling with half-built arguments would produce a payload the condition then
        # judges as if it were real.
        monkeypatch.setattr(
            "console_backend.services.watch_evaluator.token_for",
            AsyncMock(return_value="gw-token"),
        )
        call_tool = AsyncMock()
        monkeypatch.setattr("console_backend.services.watch_evaluator.call_tool", call_tool)
        outcome = await WatchEvaluator().evaluate(
            AsyncMock(), _job(check_args_exprs={"x": "string(result.a)"}), "tok"
        )
        call_tool.assert_not_awaited()
        assert outcome.condition_met is False
        assert "Dynamic arguments failed" in (outcome.error or "")


class TestJudgedConditions:
    """A judge-only watch: the model reads the whole response on every poll."""

    @pytest.mark.asyncio
    async def test_the_model_decides(self, monkeypatch):
        _gateway(monkeypatch, {"attendees": ["a@x.com"]})
        with patch(
            "console_backend.services.llm_gateway.gateway_chat",
            AsyncMock(return_value='{"condition_met": true, "reasoning": "external attendee"}'),
        ):
            with patch(
                "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "some-model"}),
            ):
                outcome = await WatchEvaluator().evaluate(
                    AsyncMock(),
                    _job(cel_expr=None, llm_condition="somebody external is invited"),
                    "tok",
                )
        assert outcome.condition_met is True
        assert outcome.evaluation.mode == "judge"
        assert outcome.evaluation.reasoning == "external attendee"

    @pytest.mark.asyncio
    async def test_the_judge_does_not_think_on_its_own_budget(self, monkeypatch):
        """The judge is the one caller here that makes a semantic decision, so it is the
        one where thinking is arguable — and it still runs with thinking off, deliberately:
        512 tokens is a budget a reasoning model spends on thinking and is then cut off
        inside, and a truncated judgement (swallowed as "condition not met") is worse than
        an unreasoned one. Pinned because it now comes from the helper's default."""
        _gateway(monkeypatch, {"attendees": ["a@x.com"]})
        chat = AsyncMock(return_value='{"condition_met": true, "reasoning": "yes"}')
        with patch("console_backend.services.llm_gateway.gateway_chat", chat):
            with patch(
                "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "some-model"}),
            ):
                await WatchEvaluator().evaluate(
                    AsyncMock(), _job(cel_expr=None, llm_condition="anything"), "tok"
                )
        assert chat.await_args.kwargs["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    async def test_an_unreachable_model_fails_closed(self, monkeypatch):
        # A false trigger notifies about something that did not happen, and repeats every
        # poll until somebody notices.
        _gateway(monkeypatch, {"attendees": []})
        with patch(
            "console_backend.services.llm_gateway.gateway_chat",
            AsyncMock(side_effect=RuntimeError("gateway down")),
        ):
            with patch(
                "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "some-model"}),
            ):
                outcome = await WatchEvaluator().evaluate(
                    AsyncMock(), _job(cel_expr=None, llm_condition="anything"), "tok"
                )
        assert outcome.condition_met is False
        assert "could not be judged" in outcome.evaluation.reasoning

    @pytest.mark.asyncio
    async def test_no_configured_model_fails_closed_and_says_that(self, monkeypatch):
        _gateway(monkeypatch, {"attendees": []})
        with patch(
            "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
            AsyncMock(return_value={}),
        ):
            outcome = await WatchEvaluator().evaluate(
                AsyncMock(), _job(cel_expr=None, llm_condition="anything"), "tok"
            )
        assert outcome.condition_met is False
        assert "No chat model is configured" in outcome.evaluation.reasoning

    @pytest.mark.asyncio
    async def test_an_overlong_reasoning_is_capped(self, monkeypatch):
        _gateway(monkeypatch, {"a": 1})
        long = "x" * 5000
        with patch(
            "console_backend.services.llm_gateway.gateway_chat",
            AsyncMock(return_value='{"condition_met": false, "reasoning": "' + long + '"}'),
        ):
            with patch(
                "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "m"}),
            ):
                outcome = await WatchEvaluator().evaluate(
                    AsyncMock(), _job(cel_expr=None, llm_condition="anything"), "tok"
                )
        assert len(outcome.evaluation.reasoning) == 2000

    @pytest.mark.asyncio
    async def test_a_large_extracted_value_is_not_copied_onto_every_run(self, monkeypatch):
        # The response is already stored on the job; the run only needs it readable.
        big = {"rows": ["y" * 200 for _ in range(100)]}
        _gateway(monkeypatch, big)
        with patch(
            "console_backend.services.llm_gateway.gateway_chat",
            AsyncMock(return_value='{"condition_met": false, "reasoning": "nothing"}'),
        ):
            with patch(
                "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "m"}),
            ):
                outcome = await WatchEvaluator().evaluate(
                    AsyncMock(), _job(cel_expr=None, llm_condition="anything"), "tok"
                )
        assert isinstance(outcome.evaluation.extracted, str)
        assert outcome.evaluation.extracted.endswith("… (truncated)")


class TestCelGateThenJudge:
    """cel_expr + llm_condition compose: the gate is necessary, the judge sufficient."""

    @pytest.mark.asyncio
    async def test_a_false_gate_never_asks_the_model(self, monkeypatch):
        # The point of the gate: the mechanical part of a condition must not cost an
        # LLM call on every quiet poll.
        _gateway(monkeypatch, {"items": [{"status": "OK"}]})
        judge = AsyncMock()
        with patch("console_backend.services.llm_gateway.gateway_chat", judge):
            outcome = await WatchEvaluator().evaluate(
                AsyncMock(), _job(llm_condition="the failure looks urgent"), "tok"
            )
        judge.assert_not_awaited()
        assert outcome.condition_met is False
        assert outcome.evaluation.mode == "cel+judge"
        assert outcome.evaluation.gate_met is False

    @pytest.mark.asyncio
    async def test_a_passed_gate_hands_the_model_the_extraction(self, monkeypatch):
        _gateway(monkeypatch, {"items": [{"status": "FAILED", "note": "disk full"}]})
        judge = AsyncMock(return_value='{"condition_met": true, "reasoning": "disk full is urgent"}')
        with patch("console_backend.services.llm_gateway.gateway_chat", judge):
            with patch(
                "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "m"}),
            ):
                outcome = await WatchEvaluator().evaluate(
                    AsyncMock(), _job(llm_condition="the failure looks urgent"), "tok"
                )
        assert outcome.condition_met is True
        assert outcome.evaluation == ConditionEvaluation(
            met=True,
            mode="cel+judge",
            gate_met=True,
            reasoning="disk full is urgent",
            extracted=[{"status": "FAILED", "note": "disk full"}],
        )
        # The judge received the evidence the gate matched, not just the raw response.
        prompt = judge.await_args.args[0]
        assert "disk full" in prompt
        assert "The current time is" in prompt

    @pytest.mark.asyncio
    async def test_the_judge_can_still_say_no(self, monkeypatch):
        # gate_met=True with met=False is exactly what the record must show: which
        # stage made the call.
        _gateway(monkeypatch, {"items": [{"status": "FAILED", "note": "test env"}]})
        with patch(
            "console_backend.services.llm_gateway.gateway_chat",
            AsyncMock(return_value='{"condition_met": false, "reasoning": "test env, not urgent"}'),
        ):
            with patch(
                "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "m"}),
            ):
                outcome = await WatchEvaluator().evaluate(
                    AsyncMock(), _job(llm_condition="the failure looks urgent"), "tok"
                )
        assert outcome.condition_met is False
        assert outcome.evaluation.gate_met is True
        assert outcome.evaluation.met is False


class TestJudgeKnowsTheTime:
    @pytest.mark.asyncio
    async def test_the_prompt_states_the_current_time(self, monkeypatch):
        # Without it, time-relative conditions are judged against a clock the model
        # infers from payload timestamps.
        _gateway(monkeypatch, {"events": []})
        judge = AsyncMock(return_value='{"condition_met": false, "reasoning": "nothing soon"}')
        with patch("console_backend.services.llm_gateway.gateway_chat", judge):
            with patch(
                "console_backend.services.watch_evaluator.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "m"}),
            ):
                await WatchEvaluator().evaluate(
                    AsyncMock(),
                    _job(
                        cel_expr=None,
                        llm_condition="a meeting starts within the hour",
                        timezone="Europe/Zurich",
                    ),
                    "tok",
                )
        prompt = judge.await_args.args[0]
        assert "The current time is" in prompt
        assert "Europe/Zurich" in prompt
