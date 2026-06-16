"""Tests for FinalResponseTextStripMiddleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from app.middleware.final_response_strip_middleware import (
    FinalResponseTextStripMiddleware,
    _strip_text_content,
)


@pytest.fixture
def middleware():
    return FinalResponseTextStripMiddleware()


def _final_response_tool_call(message: str = "The answer.") -> dict:
    return {
        "name": "FinalResponseSchema",
        "args": {"task_state": "completed", "message": message},
        "id": "tc-1",
        "type": "tool_call",
    }


def test_strips_string_content_when_final_response_present():
    msg = AIMessage(
        content="Duplicate preamble text", tool_calls=[_final_response_tool_call()]
    )
    assert _strip_text_content(msg) is True
    assert msg.content == ""
    # Tool call untouched
    assert msg.tool_calls[0]["name"] == "FinalResponseSchema"


def test_strips_text_blocks_but_keeps_thinking_and_tool_use():
    content = [
        {"type": "reasoning_content", "reasoning_content": {"text": "thinking..."}},
        {"type": "text", "text": "Duplicate preamble text"},
        {"type": "tool_use", "name": "FinalResponseSchema", "input": {}, "id": "tc-1"},
    ]
    msg = AIMessage(content=content, tool_calls=[_final_response_tool_call()])
    assert _strip_text_content(msg) is True
    types = [b["type"] for b in msg.content]
    assert types == ["reasoning_content", "tool_use"]


def test_leaves_message_without_final_response_untouched():
    msg = AIMessage(
        content="Narration before delegating",
        tool_calls=[{"name": "task", "args": {}, "id": "tc-2", "type": "tool_call"}],
    )
    assert _strip_text_content(msg) is False
    assert msg.content == "Narration before delegating"


def test_leaves_plain_text_answer_untouched():
    msg = AIMessage(content="A plain text-only answer")
    assert _strip_text_content(msg) is False
    assert msg.content == "A plain text-only answer"


def test_empty_content_not_marked_modified():
    msg = AIMessage(content="", tool_calls=[_final_response_tool_call()])
    assert _strip_text_content(msg) is False


@pytest.mark.asyncio
async def test_awrap_model_call_strips_model_response(middleware):
    msg = AIMessage(
        content="Duplicate preamble", tool_calls=[_final_response_tool_call()]
    )
    response = ModelResponse(result=[msg])
    handler = AsyncMock(return_value=response)
    request = MagicMock()

    result = await middleware.awrap_model_call(request, handler)

    handler.assert_awaited_once_with(request)
    assert result is response
    assert result.result[0].content == ""


@pytest.mark.asyncio
async def test_awrap_model_call_handles_bare_aimessage(middleware):
    msg = AIMessage(
        content="Duplicate preamble", tool_calls=[_final_response_tool_call()]
    )
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_model_call(MagicMock(), handler)

    assert result is msg
    assert result.content == ""


def test_wrap_model_call_sync_strips(middleware):
    msg = AIMessage(
        content="Duplicate preamble", tool_calls=[_final_response_tool_call()]
    )
    response = ModelResponse(result=[msg])
    handler = MagicMock(return_value=response)

    result = middleware.wrap_model_call(MagicMock(), handler)

    assert result is response
    assert result.result[0].content == ""
