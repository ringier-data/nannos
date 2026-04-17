"""Tests for filtering empty AI messages from conversation history.

Root cause: When Gemini calls FinalResponseSchema (return_direct=True) alongside
a non-return_direct tool (e.g. write_todos), the LangGraph routing logic routes
back to model instead of exiting. The model emits an empty AI message
(content=[], tool_calls=[], output_tokens=0) that gets checkpointed. On the next
turn, Gemini rejects with 400 "must include at least one parts field".

These tests verify that DynamicToolDispatchMiddleware strips empty AI messages
from the conversation history before sending to the model.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware
from app.models.config import GraphRuntimeContext


def _make_context(**kwargs) -> GraphRuntimeContext:
    defaults = {
        "user_id": "user-1",
        "user_sub": "sub-1",
        "name": "Test User",
        "email": "test@example.com",
    }
    defaults.update(kwargs)
    return GraphRuntimeContext(**defaults)


def _make_request(
    context: GraphRuntimeContext | None = None,
    messages: list | None = None,
    tools: list | None = None,
) -> MagicMock:
    request = MagicMock()
    request.runtime.context = context if context is not None else _make_context()
    request.messages = messages or [HumanMessage(content="hello")]
    request.tools = tools or []
    # override() returns a new mock with updated attributes
    def _override(**overrides):
        new_req = MagicMock()
        new_req.runtime = request.runtime
        new_req.messages = overrides.get("messages", request.messages)
        new_req.tools = overrides.get("tools", request.tools)
        new_req.override = request.override
        return new_req

    request.override = MagicMock(side_effect=_override)
    return request


@pytest.fixture
def middleware() -> DynamicToolDispatchMiddleware:
    return DynamicToolDispatchMiddleware()


@pytest.fixture
def mock_handler() -> AsyncMock:
    handler = AsyncMock(return_value=AIMessage(content="response"))
    return handler


class TestEmptyAIMessageFilter:
    """Tests for empty AI message filtering in awrap_model_call."""

    @pytest.mark.asyncio
    async def test_filters_empty_ai_message_from_history(
        self, middleware: DynamicToolDispatchMiddleware, mock_handler: AsyncMock
    ):
        """Empty AI message (content=[], tool_calls=[]) should be stripped."""
        messages = [
            SystemMessage(content="You are helpful"),
            HumanMessage(content="Do something"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "tc1", "name": "write_todos", "args": {}},
                    {"id": "tc2", "name": "FinalResponseSchema", "args": {}},
                ],
            ),
            ToolMessage(content="done", tool_call_id="tc1", name="write_todos"),
            ToolMessage(content="completed", tool_call_id="tc2", name="FinalResponseSchema"),
            # The problematic empty AI message produced by the unnecessary model re-invocation
            AIMessage(content=[], tool_calls=[], response_metadata={"finish_reason": "STOP"}),
            HumanMessage(content="so what?"),
        ]
        request = _make_request(messages=messages)
        await middleware.awrap_model_call(request, mock_handler)

        # First override call should be for message filtering
        msg_call = request.override.call_args_list[0]
        filtered = msg_call[1]["messages"]
        assert len(filtered) == 6  # original 7 minus 1 empty AI message

        # The empty AI message should be gone
        ai_messages = [m for m in filtered if isinstance(m, AIMessage)]
        for ai_msg in ai_messages:
            assert ai_msg.content or ai_msg.tool_calls, "Empty AI message should have been filtered"

    @pytest.mark.asyncio
    async def test_preserves_ai_message_with_content(
        self, middleware: DynamicToolDispatchMiddleware, mock_handler: AsyncMock
    ):
        """AI messages with text content should NOT be filtered."""
        messages = [
            HumanMessage(content="hello"),
            AIMessage(content="I can help with that!", tool_calls=[]),
        ]
        request = _make_request(messages=messages)
        await middleware.awrap_model_call(request, mock_handler)

        # No filtering needed — override should not be called with messages
        if request.override.called:
            override_kwargs = request.override.call_args[1]
            assert len(override_kwargs.get("messages", messages)) == 2

    @pytest.mark.asyncio
    async def test_preserves_ai_message_with_tool_calls(
        self, middleware: DynamicToolDispatchMiddleware, mock_handler: AsyncMock
    ):
        """AI messages with tool_calls but no text content should NOT be filtered."""
        messages = [
            HumanMessage(content="search for cats"),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc1", "name": "search", "args": {"q": "cats"}}],
            ),
        ]
        request = _make_request(messages=messages)
        await middleware.awrap_model_call(request, mock_handler)

        # AI message with tool_calls should be preserved (not filtered)
        if request.override.called:
            override_kwargs = request.override.call_args[1]
            filtered = override_kwargs.get("messages", messages)
            assert len(filtered) == 2

    @pytest.mark.asyncio
    async def test_filters_empty_content_list(
        self, middleware: DynamicToolDispatchMiddleware, mock_handler: AsyncMock
    ):
        """AI messages with content=[] (empty list) should be filtered."""
        messages = [
            HumanMessage(content="hello"),
            AIMessage(content=[], tool_calls=[]),
            HumanMessage(content="next question"),
        ]
        request = _make_request(messages=messages)
        await middleware.awrap_model_call(request, mock_handler)

        # First override call is for message filtering
        msg_call = request.override.call_args_list[0]
        filtered = msg_call[1]["messages"]
        assert len(filtered) == 2
        assert all(isinstance(m, HumanMessage) for m in filtered)

    @pytest.mark.asyncio
    async def test_filters_empty_string_content(
        self, middleware: DynamicToolDispatchMiddleware, mock_handler: AsyncMock
    ):
        """AI messages with content='' (empty string) and no tool_calls should be filtered."""
        messages = [
            HumanMessage(content="hello"),
            AIMessage(content="", tool_calls=[]),
            HumanMessage(content="next question"),
        ]
        request = _make_request(messages=messages)
        await middleware.awrap_model_call(request, mock_handler)

        # First override call is for message filtering
        msg_call = request.override.call_args_list[0]
        filtered = msg_call[1]["messages"]
        assert len(filtered) == 2

    @pytest.mark.asyncio
    async def test_no_filtering_when_no_empty_messages(
        self, middleware: DynamicToolDispatchMiddleware, mock_handler: AsyncMock
    ):
        """No override with messages when all AI messages have content."""
        messages = [
            HumanMessage(content="hello"),
            AIMessage(content="world"),
        ]
        request = _make_request(messages=messages)
        await middleware.awrap_model_call(request, mock_handler)

        # override is still called (for tools), but not with messages
        # since no filtering was needed
        if request.override.called:
            override_kwargs = request.override.call_args[1]
            # If messages key is present, it should have the same count
            if "messages" in override_kwargs:
                assert len(override_kwargs["messages"]) == 2

    @pytest.mark.asyncio
    async def test_sync_wrap_model_call_also_filters(
        self, middleware: DynamicToolDispatchMiddleware
    ):
        """Sync wrap_model_call should also filter empty AI messages."""
        messages = [
            HumanMessage(content="hello"),
            AIMessage(content=[], tool_calls=[]),
            HumanMessage(content="next"),
        ]
        request = _make_request(messages=messages)
        sync_handler = MagicMock(return_value=AIMessage(content="response"))

        middleware.wrap_model_call(request, sync_handler)

        request.override.assert_called()
        # First call should be for messages, second for tools — or combined
        first_call_kwargs = request.override.call_args_list[0][1]
        assert "messages" in first_call_kwargs
        assert len(first_call_kwargs["messages"]) == 2
