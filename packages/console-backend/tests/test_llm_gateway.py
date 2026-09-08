"""What console-backend's own chat helper puts on the wire.

`gateway_chat` is the utility path (conversation titling, catalog summarization, watch
params) — no langchain, one OpenAI-shaped POST. These cover the request body it builds.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ringier_a2a_sdk.cost_tracking.attribution import attribution_scope

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
    async def test_thinking_is_off_unless_a_caller_asks_for_it(self):
        """Every console-backend call through this helper is a mechanical utility call on
        a small budget, so thinking is opt-in. A caller that says nothing gets none."""
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion()))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat("hello", model="chat-low")

        body = fake_client.post.call_args.kwargs["json"]
        assert body["reasoning_effort"] == "none"
        assert body["messages"] == [{"role": "user", "content": "hello"}]

    @pytest.mark.asyncio
    async def test_an_explicit_none_leaves_the_model_to_itself(self):
        """The escape hatch from the default: send no value at all and let the model
        reason however it normally would."""
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion()))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat("hello", model="chat-low", reasoning_effort=None)

        assert "reasoning_effort" not in fake_client.post.call_args.kwargs["json"]

    @pytest.mark.asyncio
    async def test_json_calls_are_unthinking_by_default_too(self):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion("{}")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat_json("x", model="m")

        assert fake_client.post.call_args.kwargs["json"]["reasoning_effort"] == "none"


class TestSalvagingTheObject:
    """Models wrap JSON in fences and pad it with prose however firmly they are told not
    to, so the object has to be dug out of the text. This is the shared salvage — the
    copy conversation titling used to carry, and the reason it now calls through here."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply",
        [
            '{"title": "Invoice mismatch", "summary": "It does not match."}',
            'Sure!\n```json\n{"title": "Invoice mismatch", "summary": "It does not match."}\n```',
            '```\n{"title": "Invoice mismatch", "summary": "It does not match."}\n```\nHope that helps!',
        ],
    )
    async def test_fences_and_chatter_are_stripped(self, reply):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion(reply)))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            assert await llm_gateway.gateway_chat_json("x", model="m") == {
                "title": "Invoice mismatch",
                "summary": "It does not match.",
            }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply",
        [
            "",
            "   ",
            "I could not summarize that.",
            '{"broken": ',
            "[1, 2, 3]",  # valid JSON, but not an object
            '{"a": 1} and then {"b": 2}',  # the greedy match spans both and parses as neither
        ],
    )
    async def test_a_reply_with_no_object_in_it_is_empty_not_an_error(self, reply):
        # `{}` is the contract every caller answers with its own fallback.
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion(reply)))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            assert await llm_gateway.gateway_chat_json("x", model="m") == {}


class TestAttributionOnTheWire:
    """Who a gateway call bills to. Two sources, one header: an ambient scope for a caller
    that runs *as* somebody for a block of work (the scheduler's dispatch), and explicit
    `metadata` for one that names the payer per call (a request handler). The proxy's
    logger drops a record carrying no `user_sub`, so an unattributed call leaves no usage
    row at all."""

    @staticmethod
    def _stamped(fake_client) -> dict:
        raw = fake_client.post.call_args.kwargs["headers"].get("x-litellm-spend-logs-metadata")
        return json.loads(raw) if raw else {}

    @pytest.mark.asyncio
    async def test_it_stamps_the_ambient_scope(self):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion()))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            with attribution_scope(user_sub="owner-1", scheduled_job_id=42):
                await llm_gateway.gateway_chat("x", model="m")

        assert self._stamped(fake_client) == {"user_sub": "owner-1", "scheduled_job_id": 42}

    @pytest.mark.asyncio
    async def test_an_explicit_payer_wins_over_the_ambient_one(self):
        """A request handler bills the authenticated user, whatever context it inherited."""
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion()))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            with attribution_scope(user_sub="owner-1", scheduled_job_id=42):
                await llm_gateway.gateway_chat("x", model="m", metadata={"user_sub": "the-caller"})

        assert self._stamped(fake_client) == {"user_sub": "the-caller", "scheduled_job_id": 42}

    @pytest.mark.asyncio
    async def test_explicit_metadata_alone_still_works(self):
        # The four callers that name their payer per call keep working unchanged.
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion()))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat("x", model="m", metadata={"user_sub": "u1", "conversation_id": "c1"})

        assert self._stamped(fake_client) == {"user_sub": "u1", "conversation_id": "c1"}

    @pytest.mark.asyncio
    async def test_nothing_to_attribute_stamps_no_header(self):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion()))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat("x", model="m")

        assert "x-litellm-spend-logs-metadata" not in fake_client.post.call_args.kwargs["headers"]


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
    async def test_a_cut_off_reply_is_logged_for_every_caller(self, caplog):
        """Not only the JSON path: a prose caller stores what it was handed, so a
        half-sentence summary or a truncated judgement has to leave a trace here."""
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion_with("The campaign is pac", "length")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client), caplog.at_level("WARNING"):
            await llm_gateway.gateway_chat("x", model="chat-low", max_tokens=256)

        message = caplog.records[-1].getMessage()
        assert "cut off" in message
        assert "max_tokens=256" in message
        assert "The campaign is pac" in message

    @pytest.mark.asyncio
    async def test_a_reply_that_finished_logs_nothing(self, caplog):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion_with("All done.", "stop")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client), caplog.at_level("WARNING"):
            await llm_gateway.gateway_chat("x", model="chat-low")

        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_json_calls_forward_reasoning_effort(self):
        fake_client = SimpleNamespace(post=AsyncMock(return_value=_completion_with("{}", "stop")))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            await llm_gateway.gateway_chat_json("x", model="m", reasoning_effort="none")

        assert fake_client.post.call_args.kwargs["json"]["reasoning_effort"] == "none"
