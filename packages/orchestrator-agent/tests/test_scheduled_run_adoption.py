"""Tests for cross-service conversation adoption of scheduled runs.

When a conversation opens with a scheduled_run origin whose sub-agent is a
registered REMOTE agent, _resolve_scheduled_run_adoption re-resolves the job
and run server-side under the authenticated user's token and returns the
a2a_tracking seed (tracking key + the SERVER-stored conversation_id) that
makes the next delegation resume the run's conversation. Everything else —
non-remote sub-agents, unowned jobs, mismatched bindings, missing runs —
degrades to None (no adoption), never an error.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from agent_common.a2a.client_runnable import A2AClientRunnable

from app.core.agent import _resolve_scheduled_run_adoption

BACKEND_URL = "http://console-backend.test"

ORIGIN = {
    "kind": "scheduled_run",
    "context_id": "client-claimed-ctx",  # untrusted; must never be the seed
    "scheduled_job_id": 7.0,  # protobuf numbers arrive as floats
    "scheduled_job_run_id": 42.0,
    "sub_agent_id": 5.0,
    "sub_agent_name": "Report Agent",
    "prompt": "Summarize yesterday's sales.",
    "result_summary": "Sales were up 4%.",
    "scheduler_status": "success",
}

JOB = {"id": 7, "sub_agent_id": 5, "name": "daily-report"}
RUN = {"id": 42, "job_id": 7, "conversation_id": "server-run-ctx"}


def _remote_runnable(name: str = "Report Agent") -> A2AClientRunnable:
    runnable = A2AClientRunnable.__new__(A2AClientRunnable)
    # .name is a read-only property over the agent card
    runnable.agent_card = SimpleNamespace(name=name)
    return runnable


def _registry(runnable=None, sub_agent_id: int = 5) -> dict:
    entry = {
        "name": "ReportAgent",
        "description": "remote report agent",
        "runnable": runnable if runnable is not None else _remote_runnable(),
        "sub_agent_id": sub_agent_id,
    }
    return {"ReportAgent": entry}


@contextlib.contextmanager
def _patched_backend(job=JOB, run=RUN, job_status=200, run_status=200):
    """Patch httpx.AsyncClient with a console-backend stub.

    Context manager so no test can leak the global patch: the single-run
    endpoint (`.../runs/{id}`) is matched before the job endpoint.
    """

    async def fake_get(path, headers=None, **kwargs):
        response = MagicMock()
        if "/runs/" in path:
            response.status_code = run_status
            response.json.return_value = run
        else:
            response.status_code = job_status
            response.json.return_value = job
        return response

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=fake_get)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        yield mock_client


async def _resolve(origin=None, registry=None):
    return await _resolve_scheduled_run_adoption(
        origin if origin is not None else dict(ORIGIN),
        registry if registry is not None else _registry(),
        "tok",
        BACKEND_URL,
    )


class TestResolveScheduledRunAdoption:
    @pytest.mark.asyncio
    async def test_adopts_server_conversation_id_for_remote_agent(self):
        with _patched_backend() as mock_client:
            result = await _resolve()

        # Seeded from the SERVER-stored conversation_id, never the DataPart's.
        assert result == ("ReportAgent", "server-run-ctx")
        # Single-run endpoint (the run listing is capped to the newest 50),
        # both lookups authenticated as the requesting user.
        paths = [call.args[0] for call in mock_client.get.await_args_list]
        assert "/api/v1/scheduler/jobs/7/runs/42" in paths
        for call in mock_client.get.await_args_list:
            assert call.kwargs["headers"]["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_tracking_key_matches_the_runnable_reader_key(self):
        # The seed key must be the runnable's own tracking_key (name with
        # spaces stripped) — the key _extract_tracking_ids reads back.
        runnable = _remote_runnable("Report Agent")
        with _patched_backend():
            result = await _resolve(registry=_registry(runnable=runnable))
        assert result is not None
        assert result[0] == runnable.tracking_key == "ReportAgent"

    @pytest.mark.asyncio
    async def test_non_remote_sub_agent_is_not_adopted(self):
        # Local/foundry runnables checkpoint on thread keys the run never
        # used; seeding them would also desynchronize the HITL probe.
        result = await _resolve(registry=_registry(runnable=MagicMock()))
        assert result is None

    @pytest.mark.asyncio
    async def test_unregistered_sub_agent_is_not_adopted(self):
        result = await _resolve(registry=_registry(sub_agent_id=999))
        assert result is None

    @pytest.mark.asyncio
    async def test_unowned_job_is_not_adopted(self):
        # console-backend scopes jobs by user: another user's job 404s.
        with _patched_backend(job_status=404):
            result = await _resolve()
        assert result is None

    @pytest.mark.asyncio
    async def test_spoofed_sub_agent_binding_is_rejected(self):
        # A forged DataPart pointing a real (owned) job at a different
        # sub-agent must not seed that agent.
        with _patched_backend(job={"id": 7, "sub_agent_id": 8}):
            result = await _resolve()
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_run_is_not_adopted(self):
        with _patched_backend(run_status=404):
            result = await _resolve()
        assert result is None

    @pytest.mark.asyncio
    async def test_run_without_conversation_id_is_not_adopted(self):
        # Pre-propagation runs (or failed dispatches) have no stored
        # conversation on the executing side.
        with _patched_backend(run={"id": 42, "conversation_id": None}):
            result = await _resolve()
        assert result is None

    @pytest.mark.asyncio
    async def test_backend_error_degrades_to_no_adoption(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("backend down"))
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _resolve()
        assert result is None

    @pytest.mark.asyncio
    async def test_non_scheduled_run_kind_is_ignored(self):
        result = await _resolve(origin={"kind": "bug_report", "sub_agent_id": 5})
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_ids_are_ignored(self):
        origin = dict(ORIGIN)
        del origin["scheduled_job_run_id"]
        result = await _resolve(origin=origin)
        assert result is None
