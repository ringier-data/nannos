"""Tests that the orchestrator main graph wires the conversation-context gate.

The orchestrator uses a single graph per model (shared across users), so the
gate must resolve ``read_personal_file`` by name from the runtime tool_registry
rather than holding a per-user tool instance. These tests verify the gate is
present in ``_create_middleware_stack`` and configured with the channel-only
``read_personal_file`` rule, placed outermost (before DynamicToolDispatch).
"""

from unittest.mock import MagicMock

from agent_common.middleware.continue_on_truncation import ContinueOnTruncationMiddleware
from agent_common.middleware.conversation_context_tools_middleware import (
    ConversationContextToolsMiddleware,
)

from app.core.graph_factory import GraphFactory
from app.middleware import DynamicToolDispatchMiddleware


def _gate(stack):
    """The conversation-context gate, located by type (index-agnostic)."""
    return next(m for m in stack if isinstance(m, ConversationContextToolsMiddleware))


def _make_factory() -> GraphFactory:
    """Build a GraphFactory without running its heavy __init__ (no DynamoDB/Postgres)."""
    factory = object.__new__(GraphFactory)
    factory.config = MagicMock()
    factory.cost_logger = None
    factory._store_enabled = False  # store property → None
    factory._static_tools_cache = []  # mirrors __init__; get_static_tools() builds on first use
    factory._loop_detection_middleware = MagicMock()
    factory._auth_middleware = MagicMock()
    factory._retry_middleware = MagicMock()
    factory._a2a_middleware = MagicMock()
    factory._todo_middleware = MagicMock()
    return factory


def test_context_gate_is_outermost_tool_shaping_middleware():
    stack = _make_factory()._create_middleware_stack()

    # ContinueOnTruncationMiddleware is the outermost middleware overall, but it only
    # wraps the model call — it shapes no tools. The context gate must remain the
    # outermost *tool-shaping* middleware, immediately followed by DynamicToolDispatch
    # (the first tool-call handler), so the gate's injected tool flows through it.
    assert isinstance(stack[0], ContinueOnTruncationMiddleware)
    assert isinstance(stack[1], ConversationContextToolsMiddleware)
    assert isinstance(stack[2], DynamicToolDispatchMiddleware)


def test_context_gate_rules_read_personal_file_channel_only():
    stack = _make_factory()._create_middleware_stack()
    gate = _gate(stack)
    assert isinstance(gate, ConversationContextToolsMiddleware)

    assert gate._runtime_gated_tools == {"read_personal_file": frozenset({"channel"})}
    # No fixed instances — the orchestrator resolves the tool at runtime.
    assert gate._gated_tools == []


def test_gate_injects_read_personal_file_from_registry_in_channel():
    """End-to-end: the gate resolves read_personal_file from the runtime registry."""
    from unittest.mock import patch

    from langchain_core.tools import BaseTool

    gate = _gate(_make_factory()._create_middleware_stack())

    read_personal_file = MagicMock(spec=BaseTool)
    read_personal_file.name = "read_personal_file"

    request = MagicMock()
    request.tools = []
    request.runtime.context.tool_registry = {"read_personal_file": read_personal_file}

    captured = []

    def _override(**kwargs):
        new_req = MagicMock()
        new_req.tools = kwargs.get("tools", [])
        return new_req

    request.override.side_effect = _override

    with patch(
        "agent_common.middleware.conversation_context_tools_middleware.get_config",
        return_value={"metadata": {"scope": "channel"}},
    ):
        gate.wrap_model_call(request, lambda req: captured.append(req) or MagicMock())

    names = [t.name for t in captured[0].tools]
    assert "read_personal_file" in names
