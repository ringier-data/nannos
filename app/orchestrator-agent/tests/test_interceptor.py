"""Unit tests for SmartTokenInterceptor."""

from unittest.mock import AsyncMock, Mock

import pytest

from app.a2a_utils.authentication import SmartTokenInterceptor


class MockSecurityScheme:
    """Mock security scheme for testing."""

    def __init__(self, scheme_type, scheme=None, bearer_format=None, open_id_connect_url=None):
        self.type = scheme_type
        self.scheme = scheme
        self.bearer_format = bearer_format
        self.open_id_connect_url = open_id_connect_url


class MockSecuritySchemeWrapper:
    """Mock wrapper for security schemes."""

    def __init__(self, scheme):
        self.root = scheme


class MockAgentCard:
    """Mock AgentCard for testing."""

    def __init__(self, name, url, security_schemes=None, metadata=None, security=None):
        self.name = name
        self.url = url
        self.security_schemes = security_schemes
        self.metadata = metadata
        self.security = security


class TestDetectAuthScheme:
    """Test authentication scheme detection from AgentCard."""

    def test_detect_oidc_scheme(self):
        """Test detection of OpenID Connect authentication."""
        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={
                "oidc": MockSecuritySchemeWrapper(
                    MockSecurityScheme(
                        scheme_type="openIdConnect",
                        open_id_connect_url="https://auth.example.com/.well-known/openid-configuration",
                    )
                )
            },
        )

        mock_oauth_client = Mock()
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)

        auth_type, scheme_name, scheme_obj = interceptor._detect_auth_scheme(agent_card)

        assert auth_type == "oidc"
        assert scheme_name == "oidc"

    def test_detect_no_supported_scheme_raises(self):
        """Test that unsupported schemes raise ValueError."""
        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={
                "api_key": MockSecuritySchemeWrapper(
                    MockSecurityScheme(
                        scheme_type="http",
                        scheme="basic",  # Not supported
                    )
                )
            },
        )

        mock_oauth_client = Mock()
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)

        with pytest.raises(ValueError, match="does not have a supported security scheme"):
            interceptor._detect_auth_scheme(agent_card)


class TestTokenExchange:
    """Test token exchange logic with target-based configuration."""

    @pytest.mark.asyncio
    async def test_oidc_auth_exchanges_token_for_orchestrator_target(self):
        """Test that OIDC auth exchanges token with 'orchestrator' target by default."""
        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={
                "test-oidc": MockSecuritySchemeWrapper(
                    MockSecurityScheme(
                        scheme_type="openIdConnect",
                        open_id_connect_url="https://login.p.nannos.rcplus.io/realms/nannos/.well-known/openid-configuration",
                    )
                )
            },
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="orchestrator-token")
        mock_oauth_client.issuer = "https://login.p.nannos.rcplus.io/realms/nannos"

        interceptor = SmartTokenInterceptor(
            user_token="user-token",
            oauth2_client=mock_oauth_client,
        )

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}

        modified_payload, modified_kwargs = await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs,
            agent_card=agent_card,
            context=None,
        )

        # Verify token exchange was called with orchestrator target
        mock_oauth_client.exchange_token.assert_called_once_with(
            subject_token="user-token",
            target_client_id="orchestrator",
            requested_scopes=["openid", "profile", "email"],
        )
        # Verify exchanged token was added to headers
        assert modified_kwargs["headers"]["Authorization"] == "Bearer orchestrator-token"

    @pytest.mark.asyncio
    async def test_oidc_auth_exchanges_token_for_agent_creator_target(self):
        """Test that agent-creator uses its own client ID as target."""
        agent_card = MockAgentCard(
            name="agent-creator",
            url="https://example.com",
            security_schemes={
                "agent-creator": MockSecuritySchemeWrapper(
                    MockSecurityScheme(
                        scheme_type="openIdConnect",
                        open_id_connect_url="https://login.p.nannos.rcplus.io/realms/nannos/.well-known/openid-configuration",
                    )
                )
            },
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="agent-creator-token")
        mock_oauth_client.issuer = "https://login.p.nannos.rcplus.io/realms/nannos"
        interceptor = SmartTokenInterceptor(
            user_token="user-token",
            oauth2_client=mock_oauth_client,
        )

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}

        modified_payload, modified_kwargs = await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs,
            agent_card=agent_card,
            context=None,
        )

        # Verify token exchange was called with agent-creator target
        mock_oauth_client.exchange_token.assert_called_once_with(
            subject_token="user-token",
            target_client_id="agent-creator",
            requested_scopes=["openid", "profile", "email"],
        )
        # Verify exchanged token was added to headers
        assert modified_kwargs["headers"]["Authorization"] == "Bearer agent-creator-token"

    @pytest.mark.asyncio
    async def test_token_exchange_caches_per_target(self):
        """Test that exchanged tokens are cached per target client ID."""
        # First request to orchestrator target
        agent_card_1 = MockAgentCard(
            name="test-agent-1",
            url="https://example.com",
            security_schemes={
                "oidc": MockSecuritySchemeWrapper(
                    MockSecurityScheme(
                        scheme_type="openIdConnect",
                        open_id_connect_url="https://login.p.nannos.rcplus.io/realms/nannos/.well-known/openid-configuration",
                    )
                )
            },
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(side_effect=["orchestrator-token-1", "agent-creator-token-1"])
        mock_oauth_client.issuer = "https://login.p.nannos.rcplus.io/realms/nannos"

        interceptor = SmartTokenInterceptor(
            user_token="user-token",
            oauth2_client=mock_oauth_client,
        )

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}

        # First call to orchestrator target
        await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs,
            agent_card=agent_card_1,
            context=None,
        )

        # Second call to same target (should use cache)
        http_kwargs_2 = {}
        await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs_2,
            agent_card=agent_card_1,
            context=None,
        )

        # Should only call exchange_token once for orchestrator target
        assert mock_oauth_client.exchange_token.call_count == 1

        # Now call with agent-creator target
        agent_card_2 = MockAgentCard(
            name="agent-creator",
            url="https://example.com",
            security_schemes={
                "agent-creator": MockSecuritySchemeWrapper(
                    MockSecurityScheme(
                        scheme_type="openIdConnect",
                        open_id_connect_url="https://login.p.nannos.rcplus.io/realms/nannos/.well-known/openid-configuration",
                    )
                )
            },
        )

        http_kwargs_3 = {}
        await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs_3,
            agent_card=agent_card_2,
            context=None,
        )

        # Should call exchange_token again for different target
        assert mock_oauth_client.exchange_token.call_count == 2


class TestSubAgentIdHeader:
    """Test X-Sub-Agent-Id header injection."""

    @pytest.mark.asyncio
    async def test_oidc_auth_injects_sub_agent_id_header(self):
        """Test that OIDC auth adds sub_agent_id as HTTP header."""
        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={
                "test-oidc": MockSecuritySchemeWrapper(
                    MockSecurityScheme(
                        scheme_type="openIdConnect",
                        open_id_connect_url="https://login.p.nannos.rcplus.io/realms/nannos/.well-known/openid-configuration",
                    )
                )
            },
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="exchanged-token")
        mock_oauth_client.issuer = "https://login.p.nannos.rcplus.io/realms/nannos"

        interceptor = SmartTokenInterceptor(
            user_token="user-token",
            oauth2_client=mock_oauth_client,
            sub_agent_id=99,
        )

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}

        modified_payload, modified_kwargs = await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs,
            agent_card=agent_card,
            context=None,
        )

        # Verify sub_agent_id header was added
        assert modified_kwargs["headers"]["X-Sub-Agent-Id"] == "99"
        # Verify auth header was added
        assert modified_kwargs["headers"]["Authorization"] == "Bearer exchanged-token"

    @pytest.mark.asyncio
    async def test_no_sub_agent_id_header_when_not_provided(self):
        """Test that no X-Sub-Agent-Id header is added when sub_agent_id is not provided."""
        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={
                "test-oidc": MockSecuritySchemeWrapper(
                    MockSecurityScheme(
                        scheme_type="openIdConnect",
                        open_id_connect_url="https://login.p.nannos.rcplus.io/realms/nannos/.well-known/openid-configuration",
                    )
                )
            },
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="exchanged-token")
        mock_oauth_client.issuer = "https://login.p.nannos.rcplus.io/realms/nannos"
        interceptor = SmartTokenInterceptor(
            user_token="user-token",
            oauth2_client=mock_oauth_client,
            sub_agent_id=None,
        )

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}

        modified_payload, modified_kwargs = await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs,
            agent_card=agent_card,
            context=None,
        )

        # Verify only auth header was added, no sub_agent_id header
        assert modified_kwargs["headers"]["Authorization"] == "Bearer exchanged-token"
        assert "X-Sub-Agent-Id" not in modified_kwargs["headers"]


# NOTE: User context headers (X-User-Id, X-User-Email, X-User-Name) are NO LONGER injected.
# User context is now embedded in JWT claims (sub, email, name, groups) which are validated
# by JWTValidatorMiddleware at the sub-agent. This eliminates the need for separate headers
# and ensures user context is cryptographically verified.


class TestInterceptIntegration:
    """Integration tests for full intercept flow."""

    @pytest.mark.asyncio
    async def test_intercept_no_agent_card(self):
        """Test intercept with no agent card (no auth added)."""
        mock_oauth_client = Mock()
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}

        result_payload, result_kwargs = await interceptor.intercept(
            method_name="test", request_payload=request_payload, http_kwargs=http_kwargs, agent_card=None, context=None
        )

        # No headers should be added
        assert "headers" not in result_kwargs or "Authorization" not in result_kwargs.get("headers", {})

    @pytest.mark.asyncio
    async def test_intercept_no_security_schemes(self):
        """Test intercept with agent card but no security schemes."""
        agent_card = MockAgentCard(name="public-agent", url="https://example.com", security_schemes=None)

        mock_oauth_client = Mock()
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}

        result_payload, result_kwargs = await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs,
            agent_card=agent_card,
            context=None,
        )

        # No auth header should be added for public endpoint
        assert "headers" in result_kwargs  # Headers dict is created
        assert "Authorization" not in result_kwargs["headers"]

    @pytest.mark.asyncio
    async def test_intercept_unsupported_scheme_proceeds_without_auth(self, caplog):
        """Test that unsupported auth scheme proceeds without authentication."""
        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={
                "basic_auth": MockSecuritySchemeWrapper(MockSecurityScheme(scheme_type="http", scheme="basic"))
            },
        )

        mock_oauth_client = Mock()
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}

        result_payload, result_kwargs = await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs,
            agent_card=agent_card,
            context=None,
        )

        # Should log warning about unsupported scheme
        assert any("does not have a supported security scheme" in record.message for record in caplog.records)
        # Should proceed without auth
        assert "Authorization" not in result_kwargs.get("headers", {})

    @pytest.mark.asyncio
    async def test_headers_dict_initialized(self):
        """Test that headers dict is always initialized."""
        agent_card = MockAgentCard(name="public-agent", url="https://example.com")

        mock_oauth_client = Mock()
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)

        request_payload = {"jsonrpc": "2.0", "method": "test"}
        http_kwargs = {}  # No headers initially

        result_payload, result_kwargs = await interceptor.intercept(
            method_name="test",
            request_payload=request_payload,
            http_kwargs=http_kwargs,
            agent_card=agent_card,
            context=None,
        )

        # Headers dict should be initialized
        assert "headers" in result_kwargs
        assert isinstance(result_kwargs["headers"], dict)
