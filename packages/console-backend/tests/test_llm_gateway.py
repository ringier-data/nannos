"""What console-backend's own chat helper puts on the wire.

`gateway_chat` is the utility path (conversation titling, catalog summarization, watch
params) — no langchain, one OpenAI-shaped POST. These cover the request body it builds.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import console_backend.services.llm_gateway as llm_gateway


def _completion(content="ok"):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"choices": [{"message": {"content": content}}]})
    return resp


class TestGatewayChatPayload:
    @pytest.mark.asyncio
    async def test_forwards_reasoning_effort_when_asked(self):
        """Utility calls pass reasoning_effort="none" to keep a reasoning model from
        billing thinking tokens for work that needs no reasoning."""
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion()))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat("label this", model="chat-low", reasoning_effort="none")

        body = fake_client.post.call_args.kwargs["json"]
        assert body["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    async def test_omits_reasoning_effort_by_default(self):
        """Unset means "say nothing" — the model keeps whatever it does by default,
        rather than us sending a value the provider may reject."""
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion()))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat("hello", model="chat-low")

        body = fake_client.post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body
        assert body["messages"] == [{"role": "user", "content": "hello"}]


def _completion_with(content, finish_reason):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]})
    return resp


class TestFinishReason:
    """A cut-off reply reads like a short one; only the finish reason tells them apart.

    Reasoning models spend `max_tokens` thinking and are stopped a few tokens into the
    answer. For a month that surfaced as "no JSON object in the reply" with nothing else
    to go on, because `gateway_chat` returned the text and dropped the reason.
    """

    @pytest.mark.asyncio
    async def test_the_text_carries_the_finish_reason(self):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion_with('{"a": 1', "length")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            text = await llm_gateway.gateway_chat("x", model="m")

        assert text == '{"a": 1'  # still a str to every caller
        assert text.finish_reason == "length"

    @pytest.mark.asyncio
    async def test_a_reply_cut_off_before_the_object_closes_raises(self):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion_with('{\n  "job_type": "watch",\n  "na', "length")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            with pytest.raises(llm_gateway.GatewayReplyTruncated, match="max_tokens=1024"):
                await llm_gateway.gateway_chat_json("x", model="m")

    @pytest.mark.asyncio
    async def test_a_complete_reply_that_merely_stopped_at_the_limit_is_returned(self):
        # finish_reason=length with a whole object in it: the object is what matters.
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion_with('{"a": 1}', "length")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            assert await llm_gateway.gateway_chat_json("x", model="m") == {"a": 1}

    @pytest.mark.asyncio
    async def test_an_unusable_reply_logs_finish_reason_and_a_snippet(self, caplog):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion_with("I would rather not.", "stop")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client), caplog.at_level("WARNING"):
            assert await llm_gateway.gateway_chat_json("x", model="m") == {}

        message = caplog.records[-1].getMessage()
        assert "finish_reason=stop" in message
        assert "I would rather not." in message

    @pytest.mark.asyncio
    async def test_a_plain_string_stub_has_no_finish_reason_and_still_works(self):
        # Every existing test stubs gateway_chat with a literal; the contract holds for it.
        with patch.object(llm_gateway, "gateway_chat", AsyncMock(return_value="nothing here")):
            assert await llm_gateway.gateway_chat_json("x", model="m") == {}

    @pytest.mark.asyncio
    async def test_json_calls_forward_reasoning_effort(self):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion_with("{}", "stop")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat_json("x", model="m", reasoning_effort="none")

        assert fake_client.post.call_args.kwargs["json"]["reasoning_effort"] == "none"
