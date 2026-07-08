"""Integration test for the execute-only pipeline (Embedded Nannos, ADR-0004 step 4).

Unlike test_stream_subagent_adapter (which drives a fake runnable to check the
translation table), this wires a REAL DynamicLocalAgentRunnable — with
``client_action_enabled=True`` — through ``OrchestratorDeepAgent.stream_subagent``.
The only mocked boundary is the compiled LangGraph (``build_sub_agent_graph`` +
``retrieve_final_state``, the same seam the agent-common suite mocks), so the test
exercises the full chain that live traffic hits:

    stream_subagent → runnable.astream → _astream_impl custom-event handler
    → TaskUpdate(ClientActionMeta) → adapter → AgentStreamResponse(client_action)

This is the highest-fidelity check possible without a running orchestrator +
console-backend directive (step 5) + a provisioned cockpit sub-agent (step 6).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import Part, TaskState
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.models import LocalLangGraphSubAgentConfig
from agent_common.a2a.structured_response import SubAgentResponseSchema
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable

from app.core.agent import OrchestratorDeepAgent


def _embedded_runnable():
    cfg = LocalLangGraphSubAgentConfig(
        type="langgraph",
        name="cockpit",
        description="Embedded cockpit assistant",
        system_prompt="You are the cockpit assistant.",
        client_action_enabled=True,
    )
    return DynamicLocalAgentRunnable(config=cfg, model=MagicMock())


def _mock_graph_emitting(*custom_events):
    """A mock compiled graph whose astream yields the given v2 custom stream parts."""
    graph = AsyncMock()
    graph.with_config = MagicMock(return_value=graph)

    async def astream(*args, **kwargs):
        for evt in custom_events:
            yield {"type": "custom", "ns": (), "data": evt}

    graph.astream = astream
    no_interrupts = MagicMock()
    no_interrupts.interrupts = []
    graph.aget_state = AsyncMock(return_value=no_interrupts)
    return graph


@pytest.mark.asyncio
async def test_client_action_directive_flows_through_real_pipeline():
    """A client_action custom event emitted by the sub-agent graph must surface as an
    AgentStreamResponse carrying metadata['client_action'], followed by the terminal
    completed response — through the real runnable + _astream_impl + adapter."""
    agent = OrchestratorDeepAgent.__new__(OrchestratorDeepAgent)
    runnable = _embedded_runnable()

    directive = {"kind": "apply", "payload": {"name": "Spring sale", "frequencyCap": 3}}
    mock_graph = _mock_graph_emitting(("client_action", {"directive": directive}))
    final_state = {
        "messages": [MagicMock(content="I've filled in the campaign.")],
        "structured_response": SubAgentResponseSchema(
            task_state="completed",
            message="I've filled in the campaign.",
        ),
    }

    with (
        patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph),
        patch("agent_common.agents.dynamic_agent.retrieve_final_state", return_value=final_state),
    ):
        items = []
        async for item in agent.stream_subagent(
            runnable,
            message_parts=[Part(text="Fill the campaign with a frequency cap of 3.")],
            config={"metadata": {"user_id": 1, "assistant_id": "1"}},
            context_id="conv-1",
            client_objects=[{"type": "form", "id": "campaign", "fields": ["name", "frequencyCap"]}],
        ):
            items.append(item)

    client_actions = [i for i in items if (i.metadata or {}).get("client_action")]
    assert len(client_actions) == 1, f"expected one client_action item, got {[i.metadata for i in items]}"
    assert client_actions[0].metadata["client_action"] == directive

    terminal = items[-1]
    assert terminal.state == TaskState.TASK_STATE_COMPLETED
    assert terminal.content == "I've filled in the campaign."


@pytest.mark.asyncio
async def test_input_data_carries_orchestrator_conversation_id():
    """Fresh (non-resume) turns must build a SubAgentInput whose
    orchestrator_conversation_id is the conversation id, so the sub-agent thread
    resolves to {context_id}::dynamic-{name}."""
    agent = OrchestratorDeepAgent.__new__(OrchestratorDeepAgent)
    runnable = _embedded_runnable()

    captured = {}

    async def fake_astream(stream_input, config):
        captured["input"] = stream_input
        captured["config"] = config
        return
        yield  # make it an async generator

    runnable.astream = fake_astream

    async for _ in agent.stream_subagent(
        runnable,
        message_parts=[Part(text="hello")],
        config={"metadata": {}},
        context_id="conv-xyz",
        client_objects=[{"type": "form", "id": "c1"}],
    ):
        pass

    assert isinstance(captured["input"], SubAgentInput)
    assert captured["input"].orchestrator_conversation_id == "conv-xyz"
    # client_objects reached the config metadata for ClientObjectsMiddleware.
    assert captured["config"]["metadata"]["client_objects"] == [{"type": "form", "id": "c1"}]
