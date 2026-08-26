"""Unit tests for OrchestratorDeepAgent.stream_subagent adapter (Embedded Nannos, execute-only).

These cover the pure translation layer — sub-agent typed ``StreamEvent`` →
``AgentStreamResponse`` — plus the HITL ``GraphInterrupt`` → ``input_required``
mapping. The full execute-only path (graph build, resume, extensions) requires a
live orchestrator and is verified end-to-end separately; here we drive a fake
runnable so the mapping itself is exercised deterministically.
"""

import types

import pytest
from a2a.types import TaskState
from agent_common.a2a.stream_events import (
    ActivityLogMeta,
    ArtifactUpdate,
    ClientActionMeta,
    ErrorEvent,
    IntermediateOutputMeta,
    TaskResponseData,
    TaskUpdate,
    WorkPlanMeta,
)
from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt

from app.core.agent import OrchestratorDeepAgent


class _FakeRunnable:
    """Minimal stand-in exposing the two attributes stream_subagent touches."""

    name = "cockpit"

    def __init__(self, events=None, raises=None):
        self._events = events or []
        self._raises = raises

    async def astream(self, stream_input, config):  # noqa: ARG002 - signature match
        for ev in self._events:
            yield ev
        if self._raises is not None:
            raise self._raises


def _agent() -> OrchestratorDeepAgent:
    # stream_subagent only uses build_text_content + the passed runnable; no graph
    # factory work happens, so an uninitialised instance is sufficient.
    return OrchestratorDeepAgent.__new__(OrchestratorDeepAgent)


async def _collect(agent, runnable, *, resume=None, client_objects=None, config=None):
    out = []
    async for item in agent.stream_subagent(
        runnable,
        message_parts=[],
        config=config if config is not None else {},
        context_id="conv-1",
        resume=resume,
        client_objects=client_objects,
    ):
        out.append(item)
    return out


@pytest.mark.asyncio
async def test_maps_streaming_and_intermediate_chunks():
    runnable = _FakeRunnable(
        events=[
            ArtifactUpdate(content="Hello"),
            ArtifactUpdate(content="thinking…", event_metadata=IntermediateOutputMeta()),
            ArtifactUpdate(content=""),  # empty chunk is dropped
        ]
    )
    items = await _collect(_agent(), runnable)
    assert len(items) == 2
    assert items[0].content == "Hello"
    assert items[0].metadata == {"streaming_chunk": True}
    assert items[1].metadata["intermediate_output"] is True
    assert items[1].metadata["agent_name"] == "cockpit"


@pytest.mark.asyncio
async def test_maps_work_plan_client_action_and_activity_log():
    directive = {"kind": "apply", "payload": {"name": "Spring sale"}}
    runnable = _FakeRunnable(
        events=[
            TaskUpdate(event_metadata=WorkPlanMeta(todos=[{"content": "x"}])),
            TaskUpdate(event_metadata=ClientActionMeta(client_action=directive)),
            TaskUpdate(status_text="Using cockpit_api…", event_metadata=ActivityLogMeta()),
        ]
    )
    items = await _collect(_agent(), runnable)
    assert items[0].metadata["work_plan"] is True and items[0].metadata["todos"]
    assert items[1].metadata["client_action"] == directive
    assert items[2].metadata["activity_log"] is True
    assert items[2].content == "Using cockpit_api…"
    # A mechanical line carries no kind — only a notify_user note does (below).
    assert "kind" not in items[2].metadata


@pytest.mark.asyncio
async def test_maps_mid_turn_note_with_its_kind_marker():
    """A notify_user note rides the activity-log channel with kind='note', so clients
    can style the agent's own words apart from a tool label. The task stays WORKING."""
    runnable = _FakeRunnable(
        events=[
            TaskUpdate(
                status_text="Understood — I'll pull last week's numbers first.",
                event_metadata=ActivityLogMeta(kind="note"),
            )
        ]
    )
    items = await _collect(_agent(), runnable)
    assert len(items) == 1
    assert items[0].state == TaskState.TASK_STATE_WORKING
    assert items[0].metadata == {"activity_log": True, "kind": "note"}
    assert items[0].content == "Understood — I'll pull last week's numbers first."


@pytest.mark.asyncio
async def test_maps_terminal_result_to_completed():
    runnable = _FakeRunnable(
        events=[
            TaskUpdate(
                data=TaskResponseData(
                    state=TaskState.TASK_STATE_COMPLETED,
                    messages=[AIMessage(content="Done — filled the form.")],
                )
            )
        ]
    )
    items = await _collect(_agent(), runnable)
    assert len(items) == 1
    assert items[0].state == TaskState.TASK_STATE_COMPLETED
    assert items[0].content == "Done — filled the form."


@pytest.mark.asyncio
async def test_error_event_maps_to_failed():
    runnable = _FakeRunnable(events=[ErrorEvent(error="boom")])
    items = await _collect(_agent(), runnable)
    assert items[0].state == TaskState.TASK_STATE_FAILED
    assert "boom" in items[0].content


@pytest.mark.asyncio
async def test_graph_interrupt_maps_to_input_required_with_action_requests():
    action_requests = [{"name": "cockpit_write", "description": "Apply changes to the campaign?", "args": {}}]
    intr = types.SimpleNamespace(value={"action_requests": action_requests, "review_configs": []})
    runnable = _FakeRunnable(events=[ArtifactUpdate(content="working")], raises=GraphInterrupt((intr,)))
    items = await _collect(_agent(), runnable)
    assert items[-1].state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert items[-1].action_requests == action_requests
    assert "Apply changes" in items[-1].content


@pytest.mark.asyncio
async def test_graph_interrupt_maps_auth_required_with_authorize_url():
    # Regression: AuthErrorDetectionMiddleware's interrupt payload carries the
    # authorize URL separately from the tool's message (which only *references*
    # "the authorizeUrl"). The embedded path must surface the URL in both the
    # content and the metadata, like the orchestrator path does.
    intr = types.SimpleNamespace(
        value={
            "task_state": TaskState.TASK_STATE_AUTH_REQUIRED,
            "tool": "cockpit_get_campaign",
            "message": "This tool requires secondary authorization. Please go to the authorizeUrl.",
            "auth_url": "https://auth.example.com/oauth/begin",
            "error_code": "need-credentials",
        }
    )
    runnable = _FakeRunnable(events=[], raises=GraphInterrupt((intr,)))
    items = await _collect(_agent(), runnable)
    assert len(items) == 1
    assert items[0].state == TaskState.TASK_STATE_AUTH_REQUIRED
    assert items[0].interrupt_reason == "auth_required"
    assert "https://auth.example.com/oauth/begin" in items[0].content
    assert items[0].metadata["auth_url"] == "https://auth.example.com/oauth/begin"
    assert items[0].metadata["requires_auth"] is True
    assert items[0].metadata["tool"] == "cockpit_get_campaign"


@pytest.mark.asyncio
async def test_client_objects_injected_into_config_metadata():
    runnable = _FakeRunnable(events=[])
    cfg: dict = {"metadata": {}}
    await _collect(_agent(), runnable, client_objects=[{"type": "form", "id": "c1"}], config=cfg)
    assert cfg["metadata"]["client_objects"] == [{"type": "form", "id": "c1"}]
