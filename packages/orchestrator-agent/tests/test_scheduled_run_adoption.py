"""Tests for cross-service conversation adoption of scheduled runs.

One contract, two continuity mechanisms. A conversation opened with a
scheduled_run origin is validated server-side under the authenticated user's
token (_validate_scheduled_run_origin), then mapped onto the registered
sub-agent's native continuity mechanism (_build_adoption_seed):

- remote and local/automated agents alike get
  ``a2a_tracking[key] = {"context_id": <server run ctx>}``: the next delegation
  is sent under the run's context id, which the remote server resumes on the
  wire and a local agent resumes on the ``{ctx}::dynamic-{name}`` checkpoint
  thread agent-runner wrote (``local_sub_agent_thread_id``). No checkpoint is
  copied — the orchestrator no longer probes threads for pending interrupts, so
  a local agent may run under the run's own context id.

Unowned jobs, mismatched bindings, missing runs, and foundry agents all
degrade to no adoption, never an error.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from agent_common.a2a.client_runnable import A2AClientRunnable
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable

from app.core.agent import (
    _adopted_sub_agent_ids_from_tracking,
    _build_adoption_seed,
    _validate_scheduled_run_origin,
)

BACKEND_URL = "http://console-backend.test"

ORIGIN = {
    "kind": "scheduled_run",
    "context_id": "client-claimed-ctx",  # untrusted; must never be used
    "scheduled_job_id": 7.0,  # protobuf numbers arrive as floats
    "scheduled_job_run_id": 42.0,
    "sub_agent_id": 5.0,
    "sub_agent_name": "report-agent",
    "prompt": "Summarize yesterday's sales.",
    "result_summary": "Sales were up 4%.",
    "scheduler_status": "success",
}

JOB = {"id": 7, "sub_agent_id": 5, "name": "daily-report"}
RUN = {"id": 42, "job_id": 7, "conversation_id": "server-run-ctx"}

VALIDATED = {"sub_agent_id": 5, "conversation_id": "server-run-ctx", "job_id": 7, "run_id": 42}


def _remote_runnable(name: str = "Report Agent") -> A2AClientRunnable:
    runnable = A2AClientRunnable.__new__(A2AClientRunnable)
    # .name is a read-only property over the agent card
    runnable.agent_card = SimpleNamespace(name=name)
    return runnable


def _local_runnable(name: str = "report-agent") -> DynamicLocalAgentRunnable:
    runnable = DynamicLocalAgentRunnable.__new__(DynamicLocalAgentRunnable)
    # .name is a read-only property over the agent config
    runnable.config = SimpleNamespace(name=name)
    return runnable


def _registry(runnable, key: str = "report-agent", sub_agent_id: int = 5) -> dict:
    return {
        key: {
            "name": key,
            "description": "an agent",
            "runnable": runnable,
            "sub_agent_id": sub_agent_id,
        }
    }


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


async def _validate(origin=None):
    return await _validate_scheduled_run_origin(
        origin if origin is not None else dict(ORIGIN), "tok", BACKEND_URL
    )


class TestValidateScheduledRunOrigin:
    @pytest.mark.asyncio
    async def test_returns_server_side_run_data(self):
        with _patched_backend() as mock_client:
            result = await _validate()

        # The SERVER-stored conversation_id, never the DataPart's.
        assert result == VALIDATED
        # Single-run endpoint (the run listing is capped to the newest 50),
        # both lookups authenticated as the requesting user.
        paths = [call.args[0] for call in mock_client.get.await_args_list]
        assert "/api/v1/scheduler/jobs/7/runs/42" in paths
        for call in mock_client.get.await_args_list:
            assert call.kwargs["headers"]["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_unowned_job_is_rejected(self):
        # console-backend scopes jobs by user: another user's job 404s.
        with _patched_backend(job_status=404):
            assert await _validate() is None

    @pytest.mark.asyncio
    async def test_spoofed_sub_agent_binding_is_rejected(self):
        # A forged DataPart pointing a real (owned) job at a different
        # sub-agent must not validate.
        with _patched_backend(job={"id": 7, "sub_agent_id": 8}):
            assert await _validate() is None

    @pytest.mark.asyncio
    async def test_unknown_run_is_rejected(self):
        with _patched_backend(run_status=404):
            assert await _validate() is None

    @pytest.mark.asyncio
    async def test_run_without_conversation_id_is_rejected(self):
        # Pre-propagation runs (or failed dispatches) have no stored
        # conversation on the executing side.
        with _patched_backend(run={"id": 42, "conversation_id": None}):
            assert await _validate() is None

    @pytest.mark.asyncio
    async def test_backend_error_degrades_to_none(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("backend down"))
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            assert await _validate() is None

    @pytest.mark.asyncio
    async def test_non_scheduled_run_kind_is_ignored(self):
        assert await _validate({"kind": "bug_report", "sub_agent_id": 5}) is None

    @pytest.mark.asyncio
    async def test_missing_ids_are_ignored(self):
        origin = dict(ORIGIN)
        del origin["scheduled_job_run_id"]
        assert await _validate(origin) is None


class TestBuildAdoptionSeed:
    def test_remote_agent_gets_context_id_record(self):
        runnable = _remote_runnable("Report Agent")
        seed = _build_adoption_seed(dict(VALIDATED), _registry(runnable, key="ReportAgent"))
        assert seed is not None
        registry_key, tracking_key, record = seed
        assert registry_key == "ReportAgent"
        # The seed key must be the runnable's own tracking_key (name with
        # spaces stripped) — the key _extract_tracking_ids reads back.
        assert tracking_key == runnable.tracking_key == "ReportAgent"
        assert record == {"context_id": "server-run-ctx", "is_complete": True, "sub_agent_id": 5}

    def test_local_agent_gets_the_same_context_id_record(self):
        runnable = _local_runnable("report-agent")
        seed = _build_adoption_seed(dict(VALIDATED), _registry(runnable))
        assert seed is not None
        registry_key, tracking_key, record = seed
        assert registry_key == tracking_key == "report-agent"
        assert record == {"context_id": "server-run-ctx", "is_complete": True, "sub_agent_id": 5}

    def test_local_agent_continues_the_thread_agent_runner_wrote(self):
        """The seeded context id names the thread agent-runner executed the run on."""
        from agent_common.a2a.threads import local_sub_agent_thread_id
        from agent_common.a2a.base import SubAgentInput

        runnable = _local_runnable("report-agent")
        _, tracking_key, record = _build_adoption_seed(dict(VALIDATED), _registry(runnable))
        sub_input = SubAgentInput(messages=[], a2a_tracking={tracking_key: record})
        context_id, task_id = DynamicLocalAgentRunnable._extract_tracking_ids(runnable, sub_input)
        assert task_id is None
        assert DynamicLocalAgentRunnable.get_thread_id(runnable, context_id, sub_input) == local_sub_agent_thread_id(
            "server-run-ctx", "report-agent"
        )

    def test_foundry_like_runnable_is_not_adopted(self):
        # Anything without an adoptable continuity mechanism degrades to None.
        assert _build_adoption_seed(dict(VALIDATED), _registry(MagicMock())) is None

    def test_unregistered_sub_agent_is_not_adopted(self):
        registry = _registry(_local_runnable(), sub_agent_id=999)
        assert _build_adoption_seed(dict(VALIDATED), registry) is None


class TestAdoptedIdsFromTracking:
    """Adopted ids must survive beyond the first turn: later turns (and HITL
    resumes) rebuild the runtime context from the persisted a2a_tracking
    record, not from the origin DataPart — otherwise the adopted automated
    agent deregisters after turn one and its own interrupt's approval is
    silently dropped (round-3 review finding)."""

    def test_recovers_ids_from_adoption_records(self):
        tracking = {
            "report-agent": {"context_id": "run-ctx", "is_complete": True, "sub_agent_id": 5},
            "RemoteAgent": {"context_id": "other-ctx", "is_complete": True, "sub_agent_id": 9},
        }
        assert _adopted_sub_agent_ids_from_tracking(tracking) == {5, 9}

    def test_ordinary_tracking_records_yield_none(self):
        # Records written by the tracking middleware for normal delegations
        # carry no sub_agent_id — no adoption, no gating change.
        tracking = {
            "some-agent": {"context_id": "ctx", "task_id": "t1", "is_complete": False},
        }
        assert _adopted_sub_agent_ids_from_tracking(tracking) is None

    def test_empty_and_malformed_state_yield_none(self):
        assert _adopted_sub_agent_ids_from_tracking({}) is None
        assert _adopted_sub_agent_ids_from_tracking({"x": "not-a-dict", "y": {"sub_agent_id": "abc"}}) is None
