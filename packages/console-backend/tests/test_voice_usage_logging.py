"""Voice-call usage logging and its attribution.

The voice agent talks to Gemini Live directly, so its spend never reaches the Model
Gateway's cost logger. It reports token counts to `POST /voice/sessions/{id}/usage`.

The load-bearing property is attribution: costs must land on the sub-agent the call
impersonated, derived server-side from the session record — never from the request body.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import console_backend.routers.voice_agent_router as router
from console_backend.models.voice_session import (
    VoiceSession,
    VoiceSessionStatus,
    VoiceUsageEntry,
    VoiceUsageReport,
)

_STARTED = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def _session(**overrides) -> VoiceSession:
    """A completed call impersonating sub-agent 42."""
    fields = {
        "id": "11111111-1111-1111-1111-111111111111",
        "user_id": "user-abc",
        "sub_agent_id": 42,
        "phone_number": "+41790000000",
        "call_sid": "CA123",
        "status": VoiceSessionStatus.COMPLETED,
        "started_at": _STARTED,
        "ended_at": _STARTED + timedelta(seconds=90),
    }
    fields.update(overrides)
    return VoiceSession(**fields)


def _request(session: VoiceSession | None):
    """Request whose voice-session service resolves `session` for the given id."""
    state = SimpleNamespace(
        voice_session_service=SimpleNamespace(get_session=AsyncMock(return_value=session)),
        usage_service=SimpleNamespace(log_usage=AsyncMock(return_value=1)),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _db():
    return SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())


def _report() -> VoiceUsageReport:
    return VoiceUsageReport(
        entries=[
            VoiceUsageEntry(
                provider="vertex_ai",
                model_name="gemini-live-2.5-flash-native-audio",
                billing_unit_breakdown={"audio_input_tokens": 900, "audio_output_tokens": 400},
            ),
            VoiceUsageEntry(
                provider="vertex_ai",
                model_name="gemini-2.5-flash",
                billing_unit_breakdown={"base_input_tokens": 120},
            ),
        ]
    )


# ── Token usage reporting ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_usage_is_attributed_to_the_impersonated_sub_agent():
    """Acceptance criterion 1: tokens bill to the sub-agent, not the voice agent."""
    session = _session()
    request = _request(session)
    db = _db()

    out = await router.report_voice_session_usage(session.id, _report(), request, db)

    assert out["count"] == 2
    calls = request.app.state.usage_service.log_usage.await_args_list
    assert len(calls) == 2
    for call in calls:
        assert call.kwargs["user_id"] == "user-abc"
        assert call.kwargs["sub_agent_id"] == 42
        assert call.kwargs["voice_session_id"] == session.id
        # Billed at call time, not report time.
        assert call.kwargs["invoked_at"] == _STARTED
    assert calls[0].kwargs["model_name"] == "gemini-live-2.5-flash-native-audio"
    assert calls[0].kwargs["billing_unit_breakdown"] == {
        "audio_input_tokens": 900,
        "audio_output_tokens": 400,
    }
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_usage_report_carries_no_attribution_fields():
    """The agent must not be able to mis-attribute spend: the wire format has no
    user/sub-agent fields at all, so attribution can only come from the session."""
    assert not {"user_id", "user_sub", "sub_agent_id"} & set(VoiceUsageEntry.model_fields)


@pytest.mark.asyncio
async def test_unknown_session_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await router.report_voice_session_usage("missing", _report(), _request(None), _db())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_usage_failure_rolls_back_and_reports_500():
    request = _request(_session())
    request.app.state.usage_service.log_usage = AsyncMock(side_effect=RuntimeError("boom"))
    db = _db()

    with pytest.raises(HTTPException) as exc:
        await router.report_voice_session_usage(_session().id, _report(), request, db)

    assert exc.value.status_code == 500
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
