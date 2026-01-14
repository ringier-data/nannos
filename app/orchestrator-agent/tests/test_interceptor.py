"""Unit tests for SmartTokenInterceptor."""

from unittest.mock import AsyncMock, Mock

import pytest

from app.authentication.interceptor import SmartTokenInterceptor


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

    def test_detect_jwt_bearer_scheme(self):
        """Test detection of JWT bearer authentication."""
        # Use actual HTTPAuthSecurityScheme type to pass isinstance check
        from a2a.types import HTTPAuthSecurityScheme

        http_scheme = HTTPAuthSecurityScheme(type="http", scheme="bearer", bearer_format="JWT")

        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={"jwt_auth": MockSecuritySchemeWrapper(http_scheme)},
        )

        mock_oauth_client = Mock()
        interceptor = SmartTokenInterceptor(user_token="user-token", oauth2_client=mock_oauth_client)

        auth_type, scheme_name, scheme_obj = interceptor._detect_auth_scheme(agent_card)

        assert auth_type == "jwt"
        assert scheme_name == "jwt_auth"
        assert scheme_obj.type == "http"

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


# NOTE: The following test classes have been removed as they test private methods
# that were refactored. The functionality is now tested through integration tests.
# The current implementation adds user_context and sub_agent_id as HTTP headers
# in _handle_jwt_auth and _handle_oidc_auth methods instead of injecting into
# request payload metadata.
#
# Removed test classes:
# - TestExtractSubAgentId: tested _extract_sub_agent_id_from_card (removed method)
# - TestInjectUserContext: tested _inject_user_context (removed method)
# - TestInjectSubAgentId: tested _inject_sub_agent_id (removed method)


class TestHeaderInjection:
    """Test HTTP header injection for user context and sub_agent_id."""

    @pytest.mark.asyncio
    async def test_jwt_auth_injects_user_context_headers(self):
        """Test that JWT auth adds user context as HTTP headers."""
        from a2a.types import HTTPAuthSecurityScheme

        http_scheme = HTTPAuthSecurityScheme(type="http", scheme="bearer", bearer_format="JWT")

        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={"test-jwt": MockSecuritySchemeWrapper(http_scheme)},
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.get_token = AsyncMock(return_value="mock-jwt-token")

        user_context = {
            "user_id": "user-123",
            "email": "test@example.com",
            "name": "Test User",
        }

        interceptor = SmartTokenInterceptor(
            user_token="user-token",
            oauth2_client=mock_oauth_client,
            user_context=user_context,
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

        # Verify user context headers were added
        assert modified_kwargs["headers"]["X-User-Id"] == "user-123"
        assert modified_kwargs["headers"]["X-User-Email"] == "test@example.com"
        assert modified_kwargs["headers"]["X-User-Name"] == "Test User"
        # Verify auth header was added
        assert modified_kwargs["headers"]["Authorization"] == "Bearer mock-jwt-token"

    @pytest.mark.asyncio
    async def test_jwt_auth_injects_sub_agent_id_header(self):
        """Test that JWT auth adds sub_agent_id as HTTP header."""
        from a2a.types import HTTPAuthSecurityScheme

        http_scheme = HTTPAuthSecurityScheme(type="http", scheme="bearer", bearer_format="JWT")

        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={"test-jwt": MockSecuritySchemeWrapper(http_scheme)},
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.get_token = AsyncMock(return_value="mock-jwt-token")

        interceptor = SmartTokenInterceptor(
            user_token="user-token",
            oauth2_client=mock_oauth_client,
            sub_agent_id=42,
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
        assert modified_kwargs["headers"]["X-Sub-Agent-Id"] == "42"
        # Verify auth header was added
        assert modified_kwargs["headers"]["Authorization"] == "Bearer mock-jwt-token"

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
                        open_id_connect_url="https://login.alloy.ch/realms/a2a/.well-known/openid-configuration",
                    )
                )
            },
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.exchange_token = AsyncMock(return_value="exchanged-token")

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
    async def test_no_headers_injected_without_user_context_or_sub_agent_id(self):
        """Test that no extra headers are added when user_context and sub_agent_id are not provided."""
        from a2a.types import HTTPAuthSecurityScheme

        http_scheme = HTTPAuthSecurityScheme(type="http", scheme="bearer", bearer_format="JWT")

        agent_card = MockAgentCard(
            name="test-agent",
            url="https://example.com",
            security_schemes={"test-jwt": MockSecuritySchemeWrapper(http_scheme)},
        )

        mock_oauth_client = AsyncMock()
        mock_oauth_client.get_token = AsyncMock(return_value="mock-jwt-token")

        interceptor = SmartTokenInterceptor(
            user_token="user-token",
            oauth2_client=mock_oauth_client,
            user_context=None,
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

        # Verify only auth header was added, no user context or sub_agent_id headers
        assert modified_kwargs["headers"]["Authorization"] == "Bearer mock-jwt-token"
        assert "X-User-Id" not in modified_kwargs["headers"]
        assert "X-User-Email" not in modified_kwargs["headers"]
        assert "X-User-Name" not in modified_kwargs["headers"]
        assert "X-Sub-Agent-Id" not in modified_kwargs["headers"]


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
