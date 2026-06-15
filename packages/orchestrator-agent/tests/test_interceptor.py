"""Unit tests for SmartTokenInterceptor (A2A v1.0+ before/after contract)."""

from unittest.mock import AsyncMock, Mock

import pytest
from a2a.client.interceptors import BeforeArgs
from a2a.types import (
    AgentCard,
    HTTPAuthSecurityScheme,
    OpenIdConnectSecurityScheme,
    SecurityScheme,
)

from agent_common.a2a.authentication import SmartTokenInterceptor

ISSUER_URL = "https://login.p.nannos.rcplus.io/realms/nannos/.well-known/openid-configuration"
ISSUER = "https://login.p.nannos.rcplus.io/realms/nannos"


def _oidc_card(name: str, scheme_name: str, url: str = ISSUER_URL) -> AgentCard:
    """Build an AgentCard advertising a single OIDC security scheme."""
    return AgentCard(
        name=name,
        security_schemes={
            scheme_name: SecurityScheme(
                open_id_connect_security_scheme=OpenIdConnectSecurityScheme(open_id_connect_url=url)
            )
        },
    )


def _http_card(name: str, scheme_name: str = "basic") -> AgentCard:
    """Build an AgentCard advertising an unsupported (HTTP basic) scheme."""
    return AgentCard(
        name=name,
        security_schemes={scheme_name: SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="basic"))},
    )


async def _run_before(interceptor: SmartTokenInterceptor, agent_card) -> dict:
    """Invoke the interceptor's before() hook and return injected request headers."""
    args = BeforeArgs(input=None, method="message/send", agent_card=agent_card, context=None)
    await interceptor.before(args)
    if args.context is None or args.context.service_parameters is None:
        return {}
    return dict(args.context.service_parameters)


class TestDetectAuthScheme:
    """Test authentication scheme detection from AgentCard."""

    def test_detect_oidc_scheme(self):
        """Test detection of OpenID Connect authentication."""
        agent_card = _oidc_card("test-agent", "oidc", url="https://auth.example.com/.well-known/openid-configuration")
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=Mock())

        auth_type, scheme_name, scheme_obj = interceptor._detect_auth_scheme(agent_card)

        assert auth_type == "oidc"
        assert scheme_name == "oidc"
        assert scheme_obj.open_id_connect_url == "https://auth.example.com/.well-known/openid-configuration"

    def test_detect_no_supported_scheme_raises(self):
        """Test that unsupported schemes raise ValueError."""
        agent_card = _http_card("test-agent", "api_key")
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=Mock())

        with pytest.raises(ValueError, match="does not have a supported security scheme"):
            interceptor._detect_auth_scheme(agent_card)


class TestTokenExchange:
    """Test token exchange logic with target-based configuration."""

    @pytest.mark.asyncio
    async def test_oidc_auth_exchanges_token_for_orchestrator_target(self):
        """Test that OIDC auth exchanges token with 'orchestrator' target by default."""
        agent_card = _oidc_card("test-agent", "test-oidc")

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="orchestrator-token")
        mock_oauth_client.issuer = ISSUER

        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)
        headers = await _run_before(interceptor, agent_card)

        mock_oauth_client.exchange_token.assert_called_once_with(
            subject_token="user-token",
            target_client_id="orchestrator",
            requested_scopes=["openid", "profile", "email"],
        )
        assert headers["Authorization"] == "Bearer orchestrator-token"

    @pytest.mark.asyncio
    async def test_oidc_auth_exchanges_token_for_agent_creator_target(self):
        """Test that agent-creator uses its own client ID as target."""
        agent_card = _oidc_card("agent-creator", "agent-creator")

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="agent-creator-token")
        mock_oauth_client.issuer = ISSUER

        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)
        headers = await _run_before(interceptor, agent_card)

        mock_oauth_client.exchange_token.assert_called_once_with(
            subject_token="user-token",
            target_client_id="agent-creator",
            requested_scopes=["openid", "profile", "email"],
        )
        assert headers["Authorization"] == "Bearer agent-creator-token"

    @pytest.mark.asyncio
    async def test_token_exchange_caches_per_target(self):
        """Test that exchanged tokens are cached per target client ID."""
        agent_card_1 = _oidc_card("test-agent-1", "oidc")

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(side_effect=["orchestrator-token-1", "agent-creator-token-1"])
        mock_oauth_client.issuer = ISSUER

        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)

        # First two calls hit the same (orchestrator) target → only one exchange
        await _run_before(interceptor, agent_card_1)
        await _run_before(interceptor, agent_card_1)
        assert mock_oauth_client.exchange_token.call_count == 1

        # A different target triggers a second exchange
        agent_card_2 = _oidc_card("agent-creator", "agent-creator")
        await _run_before(interceptor, agent_card_2)
        assert mock_oauth_client.exchange_token.call_count == 2


class TestSubAgentIdHeader:
    """Test X-Sub-Agent-Id header injection."""

    @pytest.mark.asyncio
    async def test_oidc_auth_injects_sub_agent_id_header(self):
        """Test that OIDC auth adds sub_agent_id as a request header."""
        agent_card = _oidc_card("test-agent", "test-oidc")

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="exchanged-token")
        mock_oauth_client.issuer = ISSUER

        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client, sub_agent_id=99)
        headers = await _run_before(interceptor, agent_card)

        assert headers["X-Sub-Agent-Id"] == "99"
        assert headers["Authorization"] == "Bearer exchanged-token"

    @pytest.mark.asyncio
    async def test_no_sub_agent_id_header_when_not_provided(self):
        """Test that no X-Sub-Agent-Id header is added when sub_agent_id is not provided."""
        agent_card = _oidc_card("test-agent", "test-oidc")

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="exchanged-token")
        mock_oauth_client.issuer = ISSUER

        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client, sub_agent_id=None)
        headers = await _run_before(interceptor, agent_card)

        assert headers["Authorization"] == "Bearer exchanged-token"
        assert "X-Sub-Agent-Id" not in headers


class TestInterceptIntegration:
    """Integration tests for the full before() flow."""

    @pytest.mark.asyncio
    async def test_before_no_agent_card(self):
        """before() with no agent card adds no auth."""
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=Mock())
        headers = await _run_before(interceptor, None)
        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_before_no_security_schemes(self):
        """before() with an agent card but no security schemes adds no auth."""
        agent_card = AgentCard(name="public-agent")
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=Mock())
        headers = await _run_before(interceptor, agent_card)
        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_before_unsupported_scheme_proceeds_without_auth(self, caplog):
        """An unsupported auth scheme proceeds without authentication."""
        agent_card = _http_card("test-agent", "basic_auth")
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=Mock())

        headers = await _run_before(interceptor, agent_card)

        assert any("does not have a supported security scheme" in record.message for record in caplog.records)
        assert "Authorization" not in headers
