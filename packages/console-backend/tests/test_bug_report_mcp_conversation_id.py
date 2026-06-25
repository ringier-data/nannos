"""console_create_bug_report reads conversation_id from the x-nannos-context request header
(system-injected by the orchestrator) instead of a model-visible tool param."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ringier_a2a_sdk.cost_tracking.attribution import NANNOS_CONTEXT_HEADER, context_header, set_attribution

import console_backend.routers.bug_report_mcp_tools as endpoint


def _req(headers: dict | None = None):
    create = AsyncMock(return_value="report")
    state = SimpleNamespace(bug_report_service=SimpleNamespace(create_bug_report=create))
    req = SimpleNamespace(headers=headers or {}, app=SimpleNamespace(state=state))
    return req, create


@pytest.mark.asyncio
async def test_reads_conversation_id_from_context_header():
    req, create = _req({NANNOS_CONTEXT_HEADER: '{"conversation_id": "conv-42"}'})
    await endpoint.create_bug_report_mcp(req, description="boom", task_id=None, db=None, user=SimpleNamespace())
    assert create.call_args.kwargs["conversation_id"] == "conv-42"
    assert create.call_args.kwargs["source"] == "orchestrator"


@pytest.mark.asyncio
async def test_falls_back_to_unknown_when_header_absent():
    req, create = _req()
    await endpoint.create_bug_report_mcp(req, description="boom", task_id=None, db=None, user=SimpleNamespace())
    assert create.call_args.kwargs["conversation_id"] == "unknown"


@pytest.mark.asyncio
async def test_round_trips_with_context_header_builder():
    # The exact header the orchestrator's hook produces is read back correctly.
    set_attribution(user_sub="u1", conversation_id="conv-rt")
    req, create = _req(context_header())
    await endpoint.create_bug_report_mcp(req, description="boom", task_id=None, db=None, user=SimpleNamespace())
    assert create.call_args.kwargs["conversation_id"] == "conv-rt"
