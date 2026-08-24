"""Tests for generating/refining just a watch condition.

Narrower than the job-draft generator on purpose: it sees a real payload, so every
candidate is compiled AND evaluated against it, with the error fed back — the caller
receives an expression that demonstrably works on their data, or an explicit warning.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from console_backend.models.scheduled_job import GenerateConditionRequest
from console_backend.models.user import User
from console_backend.routers.scheduler_router import generate_condition

PAYLOAD = {"events": [{"start": "2026-08-25T09:30:00+02:00", "attendees": [{"email": "a@x.extern.ch"}]}]}

GOOD = "result.events.filter(e, has(e.attendees))"
BAD_SYNTAX = "result.events.filter("
BAD_FIELD = "result.meetings.filter(m, has(m.attendees))"


def _user() -> User:
    return User.model_construct(id="u1", sub="sub-1")


def _defaults(monkeypatch, model: str | None = "std-model"):
    values = {"chat": model} if model else {}
    monkeypatch.setattr(
        "console_backend.routers.scheduler_router.ModelDefaultsRepository.get_all",
        AsyncMock(return_value=values),
    )


async def _call(monkeypatch, replies: list[str], **request):
    _defaults(monkeypatch)
    chat = AsyncMock(side_effect=replies)
    with patch("console_backend.services.llm_gateway.gateway_chat", chat):
        response = await generate_condition(
            GenerateConditionRequest(**request), AsyncMock(), _user()
        )
    return response, chat


class TestGenerateCondition:
    @pytest.mark.asyncio
    async def test_a_working_expression_is_verified_against_the_payload(self, monkeypatch):
        response, chat = await _call(
            monkeypatch,
            ['{"cel_expr": "%s", "llm_condition": null}' % GOOD],
            query="events that have attendees",
            result=PAYLOAD,
        )
        assert response.cel_expr == GOOD
        assert response.verified is True
        # The evaluation rides along so the caller shows the result with no second call.
        assert response.evaluation == {"gate": True, "extracted": PAYLOAD["events"]}
        assert chat.await_count == 1

    @pytest.mark.asyncio
    async def test_a_compile_error_is_fed_back_and_retried(self, monkeypatch):
        response, chat = await _call(
            monkeypatch,
            [
                '{"cel_expr": "%s"}' % BAD_SYNTAX,
                '{"cel_expr": "%s"}' % GOOD,
            ],
            query="events with attendees",
            result=PAYLOAD,
        )
        assert response.cel_expr == GOOD
        assert response.verified is True
        retry_prompt = chat.await_args_list[1].args[0]
        assert BAD_SYNTAX in retry_prompt
        assert "does not compile" in retry_prompt

    @pytest.mark.asyncio
    async def test_a_wrong_field_path_is_caught_by_evaluating_the_real_payload(self, monkeypatch):
        # The whole point of passing the payload: BAD_FIELD compiles fine and would
        # sail through a syntax-only check, then fail every scheduled run.
        response, chat = await _call(
            monkeypatch,
            [
                '{"cel_expr": "%s"}' % BAD_FIELD,
                '{"cel_expr": "%s"}' % GOOD,
            ],
            query="events with attendees",
            result=PAYLOAD,
        )
        assert response.cel_expr == GOOD
        retry_prompt = chat.await_args_list[1].args[0]
        assert "fails against the sample response" in retry_prompt

    @pytest.mark.asyncio
    async def test_exhausted_retries_return_the_candidate_with_a_warning(self, monkeypatch):
        response, chat = await _call(
            monkeypatch,
            ['{"cel_expr": "%s"}' % BAD_FIELD] * 3,
            query="whatever",
            result=PAYLOAD,
        )
        assert response.cel_expr == BAD_FIELD
        assert response.verified is False
        assert any("could not be verified" in n for n in response.notes)
        assert chat.await_count == 3

    @pytest.mark.asyncio
    async def test_without_a_payload_the_expression_is_only_compile_checked(self, monkeypatch):
        response, _ = await _call(
            monkeypatch,
            ['{"cel_expr": "%s"}' % BAD_FIELD],  # compiles; nothing to evaluate against
            query="events with attendees",
        )
        assert response.cel_expr == BAD_FIELD
        assert response.verified is True
        assert response.evaluation is None
        assert any("not evaluated" in n for n in response.notes)

    @pytest.mark.asyncio
    async def test_a_purely_semantic_ask_may_return_judge_only(self, monkeypatch):
        response, _ = await _call(
            monkeypatch,
            ['{"cel_expr": null, "llm_condition": "the email sounds urgent"}'],
            query="tell me when an email sounds urgent",
            result={"emails": []},
        )
        assert response.cel_expr is None
        assert response.llm_condition == "the email sounds urgent"
        assert response.verified is True

    @pytest.mark.asyncio
    async def test_refinement_hands_the_model_the_current_condition(self, monkeypatch):
        _, chat = await _call(
            monkeypatch,
            ['{"cel_expr": "%s"}' % GOOD],
            query="also exclude declined attendees",
            current_cel_expr=GOOD,
            current_llm_condition="looks external",
            result=PAYLOAD,
        )
        prompt = chat.await_args_list[0].args[0]
        assert GOOD in prompt
        assert "looks external" in prompt
        assert "refine rather than replace" in prompt

    @pytest.mark.asyncio
    async def test_the_model_is_told_the_time(self, monkeypatch):
        # Payload timestamps must read as past or future, not as training-data guesses
        # — and the prompt forbids baking a literal date into the expression.
        _, chat = await _call(
            monkeypatch,
            ['{"cel_expr": "%s"}' % GOOD],
            query="events this week",
            result=PAYLOAD,
        )
        prompt = chat.await_args_list[0].args[0]
        assert "The current date and time is" in prompt
        assert "use `now`" in prompt

    @pytest.mark.asyncio
    async def test_nothing_usable_is_a_422_not_an_empty_success(self, monkeypatch):
        with pytest.raises(HTTPException) as exc:
            await _call(
                monkeypatch,
                ['{"cel_expr": null, "llm_condition": null}'],
                query="???",
                result=PAYLOAD,
            )
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_no_configured_model_is_a_503(self, monkeypatch):
        _defaults(monkeypatch, model=None)
        with pytest.raises(HTTPException) as exc:
            await generate_condition(
                GenerateConditionRequest(query="x"), AsyncMock(), _user()
            )
        assert exc.value.status_code == 503
