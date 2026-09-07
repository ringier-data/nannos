"""dispatch_streaming tells "nobody answered" from every other error.

The a2a SDK wraps httpx errors (``from e``) before they reach a caller, so the scheduler
cannot classify by httpx type itself — the case this exists for is a runner still
restarting when the card cache is empty, which arrives as AgentCardResolutionError.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from a2a.client.errors import A2AClientError, AgentCardResolutionError
from console_backend.utils.a2a_dispatch import AgentUnreachable, _is_unreachable, dispatch_streaming


def _request() -> httpx.Request:
    return httpx.Request("GET", "http://agent-runner:8000/.well-known/agent-card.json")


def _wrapped(cause: BaseException) -> BaseException:
    try:
        raise AgentCardResolutionError("card fetch failed") from cause
    except AgentCardResolutionError as e:
        return e


class TestClassification:
    def test_a_bare_transport_error_is_unreachable(self):
        assert _is_unreachable(httpx.ConnectError("refused", request=_request()))

    def test_a_read_timeout_is_unreachable(self):
        """The 300s inter-event timeout is how a black-holed runner surfaces."""
        assert _is_unreachable(httpx.ReadTimeout("silent", request=_request()))

    def test_a_wrapped_connect_error_is_unreachable(self):
        assert _is_unreachable(_wrapped(httpx.ConnectError("refused", request=_request())))

    @pytest.mark.parametrize("code", [502, 503, 504])
    def test_a_gateway_status_is_unreachable_wrapped_or_not(self, code: int):
        err = httpx.HTTPStatusError("gw", request=_request(), response=httpx.Response(code))
        assert _is_unreachable(err)
        assert _is_unreachable(_wrapped(err))

    @pytest.mark.parametrize("code", [400, 404, 500])
    def test_an_answer_from_a_running_agent_is_not(self, code: int):
        err = httpx.HTTPStatusError("agent", request=_request(), response=httpx.Response(code))
        assert not _is_unreachable(err)
        assert not _is_unreachable(_wrapped(err))

    def test_an_sdk_error_with_no_transport_cause_is_not(self):
        assert not _is_unreachable(A2AClientError("SSE stream error event received"))


class TestDispatchStreamingRaises:
    @pytest.mark.asyncio
    async def test_unreachable_runner_raises_agent_unreachable(self):
        with patch(
            "console_backend.utils.a2a_dispatch._resolve_card",
            new=AsyncMock(side_effect=_wrapped(httpx.ConnectError("refused", request=_request()))),
        ):
            with pytest.raises(AgentUnreachable) as excinfo:
                await dispatch_streaming(
                    agent_url="http://agent-runner:8000", access_token="t", parts=[], metadata={}
                )
        assert isinstance(excinfo.value.__cause__, AgentCardResolutionError)

    @pytest.mark.asyncio
    async def test_other_errors_propagate_as_raised(self):
        with patch(
            "console_backend.utils.a2a_dispatch._resolve_card",
            new=AsyncMock(side_effect=A2AClientError("bad card")),
        ):
            with pytest.raises(A2AClientError):
                await dispatch_streaming(
                    agent_url="http://agent-runner:8000", access_token="t", parts=[], metadata={}
                )
