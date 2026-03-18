"""Tests for BedrockPromptCachingMiddleware."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from ringier_a2a_sdk.middleware.bedrock_prompt_caching import BedrockPromptCachingMiddleware


@pytest.fixture
def middleware():
    return BedrockPromptCachingMiddleware()


def _make_request(system_content, model_cls="langchain_aws.ChatBedrockConverse"):
    """Create a mock ModelRequest with the given system message content."""
    request = MagicMock()

    if model_cls == "langchain_aws.ChatBedrockConverse":
        with patch("ringier_a2a_sdk.middleware.bedrock_prompt_caching.ChatBedrockConverse") as mock_cls:
            request.model = MagicMock(spec=mock_cls)
            # Make isinstance check work
            request.model.__class__ = mock_cls
    else:
        request.model = MagicMock()

    if system_content is None:
        request.system_message = None
    else:
        request.system_message = SystemMessage(content=system_content)

    # Make override return a new mock with the overridden system_message
    def fake_override(**kwargs):
        new_req = MagicMock()
        new_req.model = request.model
        new_req.system_message = kwargs.get("system_message", request.system_message)
        return new_req

    request.override = fake_override
    return request


class TestBedrockPromptCachingMiddleware:
    def test_adds_cache_point_to_string_content(self, middleware):
        """String system message gets converted to content blocks with cachePoint."""
        from langchain_aws import ChatBedrockConverse

        request = MagicMock()
        request.model = MagicMock(spec=ChatBedrockConverse)
        request.system_message = SystemMessage(content="You are a helpful agent.")
        captured = {}

        def fake_override(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        request.override = fake_override

        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        handler.assert_called_once()
        new_sys = captured["system_message"]
        assert isinstance(new_sys, SystemMessage)
        assert isinstance(new_sys.content, list)
        assert len(new_sys.content) == 2
        assert new_sys.content[0] == {"type": "text", "text": "You are a helpful agent."}
        assert new_sys.content[1] == {"cachePoint": {"type": "default"}}

    def test_adds_cache_point_to_list_content(self, middleware):
        """List content blocks get cachePoint appended."""
        from langchain_aws import ChatBedrockConverse

        request = MagicMock()
        request.model = MagicMock(spec=ChatBedrockConverse)
        request.system_message = SystemMessage(
            content=[{"type": "text", "text": "System prompt"}]
        )
        captured = {}

        def fake_override(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        request.override = fake_override

        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        new_sys = captured["system_message"]
        assert len(new_sys.content) == 2
        assert new_sys.content[1] == {"cachePoint": {"type": "default"}}

    def test_skips_if_cache_point_already_present(self, middleware):
        """Does not duplicate cachePoint if already in content."""
        from langchain_aws import ChatBedrockConverse

        request = MagicMock()
        request.model = MagicMock(spec=ChatBedrockConverse)
        request.system_message = SystemMessage(
            content=[
                {"type": "text", "text": "Prompt"},
                {"cachePoint": {"type": "default"}},
            ]
        )
        request.override = MagicMock()

        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        # Should pass original request, not call override
        handler.assert_called_once_with(request)
        request.override.assert_not_called()

    def test_skips_non_bedrock_model(self, middleware):
        """Non-Bedrock models are passed through without modification."""
        request = MagicMock()
        request.model = MagicMock()  # Not ChatBedrockConverse
        request.system_message = SystemMessage(content="test")

        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        handler.assert_called_once_with(request)

    def test_skips_no_system_message(self, middleware):
        """No system message → no modification."""
        from langchain_aws import ChatBedrockConverse

        request = MagicMock()
        request.model = MagicMock(spec=ChatBedrockConverse)
        request.system_message = None
        captured = {}

        def fake_override(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        request.override = fake_override

        handler = MagicMock(return_value="result")
        middleware.wrap_model_call(request, handler)

        handler.assert_called_once_with(request)
        assert "system_message" not in captured

    @pytest.mark.asyncio
    async def test_async_adds_cache_point(self, middleware):
        """Async version also adds cachePoint."""
        from langchain_aws import ChatBedrockConverse

        request = MagicMock()
        request.model = MagicMock(spec=ChatBedrockConverse)
        request.system_message = SystemMessage(content="Async prompt")
        captured = {}

        def fake_override(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        request.override = fake_override

        async def async_handler(req):
            return "async_result"

        result = await middleware.awrap_model_call(request, async_handler)

        assert result == "async_result"
        new_sys = captured["system_message"]
        assert new_sys.content[1] == {"cachePoint": {"type": "default"}}
