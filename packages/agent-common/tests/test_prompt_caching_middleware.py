"""Tests for LiteLLMPromptCachingMiddleware cache-breakpoint placement.

Covers the static system-prefix breakpoint (regression) and the conversation
breakpoint on the last message (the warm-TTFT lever), including append-only
safety and the per-call/no-mutation contract.
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent_common.middleware.prompt_caching import (
    LiteLLMPromptCachingMiddleware,
    _tag_last_message,
)

CC = {"type": "ephemeral"}


def _request(system: str | None, messages):
    from langchain.agents.middleware.types import ModelRequest

    return ModelRequest(
        model=MagicMock(),
        messages=list(messages),
        system_message=SystemMessage(content=system) if system is not None else None,
    )


def _last_block_cache_control(message):
    content = message.content
    assert isinstance(content, list), f"expected list content, got {type(content)}"
    return content[-1].get("cache_control")


class TestSystemPrefixBreakpoint:
    def test_system_prefix_is_tagged(self):
        mw = LiteLLMPromptCachingMiddleware()
        out = mw._apply_caching(_request("static prefix", [HumanMessage(content="hi")]))
        assert _last_block_cache_control(out.system_message) == CC

    def test_ttl_1h_is_explicit(self):
        mw = LiteLLMPromptCachingMiddleware(ttl="1h")
        out = mw._apply_caching(_request("prefix", [HumanMessage(content="hi")]))
        assert _last_block_cache_control(out.system_message) == {"type": "ephemeral", "ttl": "1h"}


class TestConversationBreakpoint:
    def test_last_message_is_tagged_by_default(self):
        mw = LiteLLMPromptCachingMiddleware()
        msgs = [HumanMessage(content="first"), AIMessage(content="ans"), HumanMessage(content="next?")]
        out = mw._apply_caching(_request("prefix", msgs))
        assert _last_block_cache_control(out.messages[-1]) == CC

    def test_earlier_messages_untouched(self):
        """Append-only safety: only the last message is rewritten."""
        mw = LiteLLMPromptCachingMiddleware()
        msgs = [HumanMessage(content="first"), HumanMessage(content="next?")]
        out = mw._apply_caching(_request("prefix", msgs))
        assert out.messages[0] is msgs[0]
        assert out.messages[0].content == "first"

    def test_tool_message_tagged_preserves_fields(self):
        mw = LiteLLMPromptCachingMiddleware()
        msgs = [HumanMessage(content="q"), ToolMessage(content="result", tool_call_id="t1")]
        out = mw._apply_caching(_request("prefix", msgs))
        assert _last_block_cache_control(out.messages[-1]) == CC
        assert out.messages[-1].tool_call_id == "t1"

    def test_disabled_leaves_messages_untouched(self):
        mw = LiteLLMPromptCachingMiddleware(cache_conversation=False)
        msgs = [HumanMessage(content="only")]
        out = mw._apply_caching(_request("prefix", msgs))
        assert out.messages[-1] is msgs[0]
        # system prefix is still tagged
        assert _last_block_cache_control(out.system_message) == CC

    def test_empty_content_last_message_is_noop(self):
        """A tool-call-only assistant message (empty content) gets no breakpoint."""
        msgs = [HumanMessage(content="q"), AIMessage(content="", tool_calls=[])]
        assert _tag_last_message(msgs, CC) is msgs

    def test_idempotent_retag_is_noop(self):
        msgs = [HumanMessage(content="hi")]
        once = _tag_last_message(msgs, CC)
        assert _tag_last_message(once, CC) is once


class TestPerCallContract:
    def test_original_request_messages_not_mutated(self):
        mw = LiteLLMPromptCachingMiddleware()
        msgs = [HumanMessage(content="hi")]
        req = _request("prefix", msgs)
        mw._apply_caching(req)
        # original message object is unchanged (str content, no marker)
        assert msgs[0].content == "hi"

    def test_wrap_model_call_passes_tagged_request_to_handler(self):
        mw = LiteLLMPromptCachingMiddleware()
        req = _request("prefix", [HumanMessage(content="hi")])
        captured = {}
        mw.wrap_model_call(req, lambda r: captured.setdefault("req", r))
        assert _last_block_cache_control(captured["req"].system_message) == CC
        assert _last_block_cache_control(captured["req"].messages[-1]) == CC

    @pytest.mark.asyncio
    async def test_awrap_model_call_passes_tagged_request(self):
        mw = LiteLLMPromptCachingMiddleware()
        req = _request("prefix", [HumanMessage(content="hi")])

        async def handler(r):
            return r

        out = await mw.awrap_model_call(req, handler)
        assert _last_block_cache_control(out.messages[-1]) == CC


class TestVolatileContextIsSkipped:
    def test_breakpoint_lands_on_last_persisted_message(self):
        """The per-call <current_page>/<client_objects> block is appended AFTER the
        conversation and never checkpointed; a breakpoint on it would never be hit.
        The breakpoint must go on the stable message in front of it."""
        from agent_common.middleware.utils import append_volatile_context_message

        tool = ToolMessage(content="result", tool_call_id="c1")
        msgs = append_volatile_context_message(
            [HumanMessage(content="q"), AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]), tool],
            "<current_page>...</current_page>",
        )
        out = _tag_last_message(msgs, CC)
        assert _last_block_cache_control(out[2]) == CC  # the ToolMessage
        assert out[3].content == "<current_page>...</current_page>"  # block untouched (str content)
        assert out[3].additional_kwargs.get("volatile_context") is True

    def test_only_volatile_messages_is_noop(self):
        from agent_common.middleware.utils import append_volatile_context_message

        msgs = append_volatile_context_message([], "BLOCK")
        assert _tag_last_message(msgs, CC) is msgs
