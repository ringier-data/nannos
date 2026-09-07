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
