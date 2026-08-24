"""Every path out of gatana_auth exchanges the subject token for a target audience.

Gatana validates the audience and answers 401 for a token minted for any other client,
so the exchange is the whole point of this module — and it had no test. It was added by
PR #84 for the orchestrator/A2A Bearer path specifically, which is the path least likely
to be exercised by hand: a session-based click in the console goes down the other branch.

`get_user_subject_token` returning the incoming Bearer token *unexchanged* is correct —
the audience depends on which server hosts the tool being called, which only the caller
knows — but it reads like the exchange went missing, so what stops it from actually going
missing is these tests.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from console_backend.config import config
from console_backend.services.mcp_tool_client import token_for
from console_backend.utils import gatana_auth
from console_backend.utils.gatana_auth import get_gatana_token, get_user_subject_token


def gateway_audience() -> str:
    return config.mcp_gateway.client_id


def console_audience() -> str:
    return config.oidc.client_id


@pytest.fixture
def user():
    return MagicMock(email="someone@example.com")


def _request(*, bearer: str | None = None, session_token: str | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    request.state = MagicMock()
    request.state.access_token = session_token
    # Far-future expiry, so the session path does not try to refresh. The attribute name
    # matters: anything else reads as "no expiry info", which the code treats as expired.
    request.state.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return request


def _exchange_spy(returns: str = "exchanged-token") -> AsyncMock:
    """Stand in for the OIDC client, recording what audience was asked for."""
    spy = AsyncMock(return_value=returns)
    client = MagicMock()
    client.exchange_token = spy
    return spy, MagicMock(return_value=client)


class TestSubjectTokenIsNotAnAudienceToken:
    @pytest.mark.asyncio
    async def test_the_bearer_token_is_returned_unexchanged(self, user):
        # The contract of the split: identify the user, leave the audience to the caller.
        got = await get_user_subject_token(_request(bearer="incoming-agent-console-token"), user)
        assert got == "incoming-agent-console-token"

    @pytest.mark.asyncio
    async def test_a_malformed_authorization_header_is_a_401(self, user):
        request = MagicMock()
        request.headers = {"Authorization": "Token abc"}
        with pytest.raises(HTTPException) as exc:
            await get_user_subject_token(request, user)
        assert exc.value.status_code == 401


class TestGatanaTokenAlwaysExchanges:
    """PR #84's fix, for both authentication patterns."""

    @pytest.mark.asyncio
    async def test_the_bearer_path_exchanges_for_the_gateway_audience(self, user):
        spy, client_cls = _exchange_spy()
        with patch.object(gatana_auth, "OidcOAuth2Client", client_cls):
            token = await get_gatana_token(_request(bearer="incoming-agent-console-token"), user)

        assert token == "exchanged-token"
        kwargs = spy.await_args.kwargs
        assert kwargs["subject_token"] == "incoming-agent-console-token"
        assert kwargs["target_client_id"] == gateway_audience()

    @pytest.mark.asyncio
    async def test_the_session_path_exchanges_for_the_gateway_audience(self, user):
        spy, client_cls = _exchange_spy()
        with patch.object(gatana_auth, "OidcOAuth2Client", client_cls):
            token = await get_gatana_token(_request(session_token="session-user-token"), user)

        assert token == "exchanged-token"
        kwargs = spy.await_args.kwargs
        assert kwargs["subject_token"] == "session-user-token"
        assert kwargs["target_client_id"] == gateway_audience()

    @pytest.mark.asyncio
    async def test_a_refused_exchange_is_a_401_not_a_leaked_subject_token(self, user):
        client = MagicMock()
        client.exchange_token = AsyncMock(side_effect=RuntimeError("audience not permitted"))
        with patch.object(gatana_auth, "OidcOAuth2Client", MagicMock(return_value=client)):
            with pytest.raises(HTTPException) as exc:
                await get_gatana_token(_request(bearer="incoming"), user)
        assert exc.value.status_code == 401


class TestTheAudienceFollowsTheTool:
    """The reason the exchange moved to the caller in the first place."""

    @pytest.mark.asyncio
    async def test_a_gateway_tool_gets_the_gateway_audience(self):
        spy, client_cls = _exchange_spy()
        with patch.object(gatana_auth, "OidcOAuth2Client", client_cls):
            await token_for("naonous_get_campaign", "subject")
        assert spy.await_args.kwargs["target_client_id"] == gateway_audience()

    @pytest.mark.asyncio
    async def test_a_console_tool_gets_this_backend_s_audience(self):
        # This backend serves console_* itself and rejects a gateway token, which is why
        # one blanket "exchange for gatana" could not stay.
        spy, client_cls = _exchange_spy()
        with patch.object(gatana_auth, "OidcOAuth2Client", client_cls):
            await token_for("console_list_mcp_servers", "subject")
        assert spy.await_args.kwargs["target_client_id"] == console_audience()
