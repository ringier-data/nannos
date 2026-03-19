"""Tests for BedrockPromptCachingMiddleware."""

from unittest.mock import MagicMock

import pytest
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ringier_a2a_sdk.middleware.bedrock_prompt_caching import BedrockPromptCachingMiddleware


@pytest.fixture
def middleware():
    return BedrockPromptCachingMiddleware()


def _make_request(system_content=None, messages=None):
    """Create a mock ModelRequest with given system message and messages."""

    def _build(model, system_message, msgs):
        req = MagicMock()
        req.model = model
        req.system_message = system_message
        req.messages = msgs

        def fake_override(**kwargs):
            return _build(
                model=model,
                system_message=kwargs.get("system_message", req.system_message),
                msgs=kwargs.get("messages", req.messages),
            )

        req.override = fake_override
        return req

    model = MagicMock(spec=ChatBedrockConverse)
    system_message = None if system_content is None else SystemMessage(content=system_content)
    return _build(model, system_message, messages or [])


class TestSystemPromptCaching:
    def test_adds_cache_point_to_string_content(self, middleware):
        """String system message gets converted to content blocks with cachePoint."""
        request = _make_request(system_content="You are a helpful agent.")
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        handler.assert_called_once()
        called_req = handler.call_args[0][0]
        new_sys = called_req.system_message
        assert isinstance(new_sys, SystemMessage)
        assert isinstance(new_sys.content, list)
        assert len(new_sys.content) == 2
        assert new_sys.content[0] == {"type": "text", "text": "You are a helpful agent."}
        assert new_sys.content[1] == {"cachePoint": {"type": "default"}}

    def test_adds_cache_point_to_list_content(self, middleware):
        """List content blocks get cachePoint appended."""
        request = _make_request(
            system_content=[{"type": "text", "text": "System prompt"}]
        )
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        new_sys = called_req.system_message
        assert len(new_sys.content) == 2
        assert new_sys.content[1] == {"cachePoint": {"type": "default"}}

    def test_skips_if_cache_point_already_present(self, middleware):
        """Does not duplicate cachePoint if already in content."""
        request = _make_request(
            system_content=[
                {"type": "text", "text": "Prompt"},
                {"cachePoint": {"type": "default"}},
            ]
        )
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        # Should pass through without adding another cache point to system message
        handler.assert_called_once()

    def test_skips_non_bedrock_model(self, middleware):
        """Non-Bedrock models are passed through without modification."""
        request = MagicMock()
        request.model = MagicMock()  # Not ChatBedrockConverse
        request.system_message = SystemMessage(content="test")

        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        handler.assert_called_once_with(request)

    def test_skips_no_system_message(self, middleware):
        """No system message → no system cache point."""
        request = _make_request(
            system_content=None,
            messages=[HumanMessage(content="hello")],
        )
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        # Should still be called (just won't modify system message)
        handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_adds_cache_point(self, middleware):
        """Async version also adds cachePoint."""
        request = _make_request(system_content="Async prompt")

        async def async_handler(req):
            return "async_result"

        result = await middleware.awrap_model_call(request, async_handler)

        assert result == "async_result"


class TestConversationHistoryCaching:
    def test_adds_cache_point_to_second_to_last_message(self, middleware):
        """Cache point is added to the message before the latest user message."""
        messages = [
            HumanMessage(content="What is Python?"),
            AIMessage(content="Python is a programming language."),
            HumanMessage(content="Tell me more."),
        ]
        request = _make_request(system_content="You are helpful.", messages=messages)
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        cached_msg = called_req.messages[1]  # The AIMessage
        assert isinstance(cached_msg.content, list)
        assert cached_msg.content[-1] == {"cachePoint": {"type": "default"}}
        assert cached_msg.content[0] == {"type": "text", "text": "Python is a programming language."}

    def test_skips_single_message(self, middleware):
        """Only one message (current user query) → no conversation cache point."""
        messages = [HumanMessage(content="Hello")]
        request = _make_request(system_content="System", messages=messages)
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        # The single message should not be modified
        assert called_req.messages[0].content == "Hello"

    def test_skips_empty_messages(self, middleware):
        """Empty message list → no conversation cache point."""
        request = _make_request(system_content="System", messages=[])
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)
        handler.assert_called_once()

    def test_preserves_ai_message_metadata(self, middleware):
        """AIMessage metadata and tool_calls are preserved."""
        ai_msg = AIMessage(
            content="I'll search for that.",
            tool_calls=[{"name": "search", "args": {"q": "test"}, "id": "tc1", "type": "tool_call"}],
            response_metadata={"model_id": "anthropic.claude-3-5-sonnet"},
        )
        messages = [
            HumanMessage(content="Search for test"),
            ai_msg,
            HumanMessage(content="What did you find?"),
        ]
        request = _make_request(system_content="System", messages=messages)
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        cached_msg = called_req.messages[1]
        assert isinstance(cached_msg, AIMessage)
        assert cached_msg.tool_calls == ai_msg.tool_calls
        assert cached_msg.response_metadata == ai_msg.response_metadata
        assert cached_msg.content[-1] == {"cachePoint": {"type": "default"}}

    def test_handles_list_content_in_conversation(self, middleware):
        """Messages with list content blocks get cachePoint appended."""
        ai_msg = AIMessage(
            content=[
                {"type": "text", "text": "Here is what I found:"},
                {"type": "text", "text": "Result details..."},
            ]
        )
        messages = [
            HumanMessage(content="Search"),
            ai_msg,
            HumanMessage(content="More?"),
        ]
        request = _make_request(system_content="System", messages=messages)
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        cached_msg = called_req.messages[1]
        assert len(cached_msg.content) == 3
        assert cached_msg.content[2] == {"cachePoint": {"type": "default"}}

    def test_skips_if_conversation_cache_point_already_present(self, middleware):
        """Does not duplicate cachePoint on conversation messages."""
        ai_msg = AIMessage(
            content=[
                {"type": "text", "text": "Response"},
                {"cachePoint": {"type": "default"}},
            ]
        )
        messages = [
            HumanMessage(content="First"),
            ai_msg,
            HumanMessage(content="Second"),
        ]
        request = _make_request(system_content="System", messages=messages)
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        # Should not add another cache point to the AI message
        called_req = handler.call_args[0][0]
        cached_msg = called_req.messages[1]
        cache_points = [b for b in cached_msg.content if isinstance(b, dict) and "cachePoint" in b]
        assert len(cache_points) == 1

    def test_works_with_tool_messages(self, middleware):
        """Cache point goes on second-to-last even if it's a ToolMessage."""
        messages = [
            HumanMessage(content="Search for X"),
            AIMessage(content="Searching...", tool_calls=[{"name": "search", "args": {}, "id": "tc1", "type": "tool_call"}]),
            ToolMessage(content="Search result: found X", tool_call_id="tc1"),
            HumanMessage(content="Great, tell me more"),
        ]
        request = _make_request(system_content="System", messages=messages)
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        # ToolMessage at index 2 should get the cache point
        cached_msg = called_req.messages[2]
        assert isinstance(cached_msg, ToolMessage)
        assert cached_msg.content[-1] == {"cachePoint": {"type": "default"}}

    def test_both_system_and_conversation_cache_points(self, middleware):
        """Both system prompt and conversation history get cache points."""
        messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
            HumanMessage(content="How are you?"),
        ]
        request = _make_request(system_content="Be helpful.", messages=messages)
        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        # System message has cache point
        sys_content = called_req.system_message.content
        assert sys_content[-1] == {"cachePoint": {"type": "default"}}
        # Conversation message has cache point
        conv_content = called_req.messages[1].content
        assert conv_content[-1] == {"cachePoint": {"type": "default"}}

    @pytest.mark.asyncio
    async def test_async_conversation_cache_point(self, middleware):
        """Async version also adds conversation cache point."""
        messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
            HumanMessage(content="Bye"),
        ]
        request = _make_request(system_content="System", messages=messages)

        async def async_handler(req):
            return "async_result"

        result = await middleware.awrap_model_call(request, async_handler)
        assert result == "async_result"

    def test_min_messages_threshold(self):
        """Conversation caching respects configurable min_messages threshold."""
        mw = BedrockPromptCachingMiddleware(min_messages=4)
        messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
            HumanMessage(content="More"),
        ]
        request = _make_request(system_content="System", messages=messages)
        handler = MagicMock(return_value="result")
        mw.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        # 3 messages < min_messages=4, so no conversation cache point
        assert called_req.messages[1].content == "Hello!"

        # Now with 5 messages (>= 4), it should cache
        messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
            HumanMessage(content="More"),
            AIMessage(content="Sure!"),
            HumanMessage(content="Thanks"),
        ]
        request = _make_request(system_content="System", messages=messages)
        mw.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        cached_msg = called_req.messages[3]  # second-to-last
        assert cached_msg.content[-1] == {"cachePoint": {"type": "default"}}

    def test_disable_system_prompt_caching(self):
        """cache_system_prompt=False skips system message cache point."""
        mw = BedrockPromptCachingMiddleware(cache_system_prompt=False)
        messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
            HumanMessage(content="More"),
        ]
        request = _make_request(system_content="Be helpful.", messages=messages)
        handler = MagicMock(return_value="result")
        mw.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        # System message unchanged (still a string)
        assert called_req.system_message.content == "Be helpful."
        # Conversation cache point still applied
        assert called_req.messages[1].content[-1] == {"cachePoint": {"type": "default"}}

    def test_disable_conversation_caching(self):
        """cache_conversation=False skips conversation cache point."""
        mw = BedrockPromptCachingMiddleware(cache_conversation=False)
        messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
            HumanMessage(content="More"),
        ]
        request = _make_request(system_content="Be helpful.", messages=messages)
        handler = MagicMock(return_value="result")
        mw.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        # System message has cache point
        assert called_req.system_message.content[-1] == {"cachePoint": {"type": "default"}}
        # Conversation messages unchanged
        assert called_req.messages[1].content == "Hello!"

    def test_disable_all_caching(self):
        """Both flags False → middleware is a no-op for Bedrock models."""
        mw = BedrockPromptCachingMiddleware(cache_system_prompt=False, cache_conversation=False)
        messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
            HumanMessage(content="More"),
        ]
        request = _make_request(system_content="Be helpful.", messages=messages)
        handler = MagicMock(return_value="result")
        mw.wrap_model_call(request, handler)

        called_req = handler.call_args[0][0]
        assert called_req.system_message.content == "Be helpful."
        assert called_req.messages[1].content == "Hello!"
