"""What the dispatch harvests off an agent's stream.

``BaseAgentExecutor`` publishes the terminal payload in two different places depending
on the state: ``completed`` gets an artifact, while ``failed``, ``auth_required`` and
``input_required`` carry it in the STATUS MESSAGE and add no artifact at all. Reading
artifacts alone therefore dropped the entire result of every non-completed run — a
failed run reached the scheduler with no error text, and a parked run lost the ask, the
task id and the reply target, so it was recorded as an ordinary success.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from a2a.types import TaskState

from console_backend.utils.a2a_dispatch import dispatch_streaming


class _Field:
    """Minimal stand-in for the proto chunk API the dispatch reads."""

    def __init__(self, kind: str, **fields):
        self._kind = kind
        for k, v in fields.items():
            setattr(self, k, v)

    def WhichOneof(self, _name: str) -> str:  # noqa: N802 - proto API shape
        return self._kind


def _text_part(text: str):
    return SimpleNamespace(text=text, WhichOneof=lambda _n: "text")


def _status_chunk(state, text: str | None, context_id: str = "ctx-1"):
    message = SimpleNamespace(parts=[_text_part(text)] if text else [])
    status = SimpleNamespace(
        state=state,
        message=message,
        HasField=lambda _n, present=text is not None: present,
    )
    return _Field("status_update", status_update=SimpleNamespace(status=status, context_id=context_id))


def _artifact_chunk(text: str, context_id: str = "ctx-1"):
    return _Field(
        "artifact_update",
        artifact_update=SimpleNamespace(
            context_id=context_id, append=False, artifact=SimpleNamespace(parts=[_text_part(text)])
        ),
    )


async def _dispatch(chunks):
    async def _send(_request):
        for chunk in chunks:
            yield chunk

    with (
        patch("console_backend.utils.a2a_dispatch._resolve_card", AsyncMock(return_value=object())),
        patch("console_backend.utils.a2a_dispatch.ClientFactory") as factory,
    ):
        factory.return_value.create.return_value = SimpleNamespace(send_message=_send)
        return await dispatch_streaming(
            agent_url="http://agent-runner", access_token="t", parts=[], metadata={}
        )


def _artifact_text(result: dict) -> str | None:
    artifacts = result["result"]["artifacts"]
    return artifacts[0]["parts"][0]["text"] if artifacts else None


class TestStatusCarriedPayloads:
    @pytest.mark.asyncio
    async def test_a_parked_runs_ask_survives_the_stream(self):
        """Without this the whole ADR-0009 mechanism is silently inert.

        The parked payload names the task the owner's answer is addressed to and the ask
        to put to them. Dropped, the scheduler sees an empty result and records the run
        as a success — the job neither stops nor asks.
        """
        payload = {
            "scheduler_status": "auth_required",
            "parked_task_id": "outer-task-1",
            "auth_payload": {"requires_auth": True},
        }
        result = await _dispatch(
            [
                _status_chunk(TaskState.TASK_STATE_WORKING, "Executing scheduled job..."),
                _status_chunk(TaskState.TASK_STATE_AUTH_REQUIRED, json.dumps(payload)),
            ]
        )
        assert json.loads(_artifact_text(result)) == payload
        assert result["result"]["status"]["state"] == "auth_required"

    @pytest.mark.asyncio
    async def test_a_failed_runs_error_text_survives_the_stream(self):
        payload = {"scheduler_status": "failed", "error_message": "No module named 'greenlet'"}
        result = await _dispatch([_status_chunk(TaskState.TASK_STATE_FAILED, json.dumps(payload))])
        assert json.loads(_artifact_text(result))["error_message"] == "No module named 'greenlet'"
        assert result["result"]["status"]["state"] == "failed"

    @pytest.mark.asyncio
    async def test_a_working_status_never_becomes_the_result(self):
        """A progress line is not an outcome, however late it arrives."""
        result = await _dispatch(
            [
                _artifact_chunk("The real answer."),
                _status_chunk(TaskState.TASK_STATE_WORKING, "Still going..."),
                _status_chunk(TaskState.TASK_STATE_COMPLETED, None),
            ]
        )
        assert _artifact_text(result) == "The real answer."

    @pytest.mark.asyncio
    async def test_a_completed_runs_artifact_is_still_what_wins(self):
        result = await _dispatch(
            [_artifact_chunk("Daily report generated."), _status_chunk(TaskState.TASK_STATE_COMPLETED, None)]
        )
        assert _artifact_text(result) == "Daily report generated."
        assert result["result"]["status"]["state"] == "completed"
