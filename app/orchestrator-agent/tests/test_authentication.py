"""Unit tests for authentication middleware components."""

import os
from unittest.mock import AsyncMock, Mock

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.middleware.oidc_auth_middleware import OidcAuthMiddleware


class TestOidcAuthMiddleware:
    """Tests for OidcAuthMiddleware."""

    def test_middleware_initialization(self):
        """Test middleware initialization with default values."""
        app = Mock()
        middleware = OidcAuthMiddleware(app)

        # The middleware now uses issuer instead of separate oidc_domain
        assert middleware.issuer == os.getenv("OIDC_ISSUER")
        assert middleware.client_id == os.getenv("OIDC_CLIENT_ID")

    def test_middleware_initialization_with_custom_values(self):
        """Test middleware initialization with custom values."""
        app = Mock()
        middleware = OidcAuthMiddleware(
            app,
            issuer="https://custom.oidc.com/oauth2/default",
            client_id="test-client-id",
        )

        assert middleware.issuer == "https://custom.oidc.com/oauth2/default"
        assert middleware.client_id == "test-client-id"
        assert middleware.issuer == "https://custom.oidc.com/oauth2/default"

    def test_public_paths_defined(self):
        """Test that public paths are properly defined."""
        app = Mock()
        middleware = OidcAuthMiddleware(app)

        assert "/.well-known/agent-card.json" in middleware.PUBLIC_PATHS
        assert "/health" in middleware.PUBLIC_PATHS
        assert "/docs" in middleware.PUBLIC_PATHS
        assert "/openapi.json" in middleware.PUBLIC_PATHS


class TestOidcAuthMiddlewareDispatch:
    """Tests for OidcAuthMiddleware request dispatch."""

    async def test_public_path_allows_access(self):
        """Test that public paths don't require authentication."""
        app = Mock()
        middleware = OidcAuthMiddleware(app)

        # Mock request for public path
        request = Mock(spec=Request)
        request.url.path = "/.well-known/agent-card.json"

        # Mock call_next
        expected_response = Response()
        call_next = AsyncMock(return_value=expected_response)

        response = await middleware.dispatch(request, call_next)

        # Should pass through without authentication
        assert response == expected_response
        call_next.assert_called_once_with(request)

    async def test_missing_authorization_header(self):
        """Test that missing Authorization header returns 401."""
        app = Mock()
        middleware = OidcAuthMiddleware(app)

        # Mock request without Authorization header
        request = Mock(spec=Request)
        request.url.path = "/api/test"
        request.headers.get = Mock(return_value=None)

        call_next = AsyncMock()

        response = await middleware.dispatch(request, call_next)

        # Should return 401
        assert isinstance(response, JSONResponse)
        assert response.status_code == 401
        call_next.assert_not_called()

    async def test_invalid_authorization_format(self):
        """Test that invalid Authorization format returns 401."""
        app = Mock()
        middleware = OidcAuthMiddleware(app)

        # Mock request with invalid Authorization format
        request = Mock(spec=Request)
        request.url.path = "/api/test"
        request.headers.get = Mock(return_value="InvalidFormat")

        call_next = AsyncMock()

        response = await middleware.dispatch(request, call_next)

        # Should return 401
        assert isinstance(response, JSONResponse)
        assert response.status_code == 401

    async def test_single_word_authorization_header(self):
        """Test that single-word Authorization header returns 401."""
        app = Mock()
        middleware = OidcAuthMiddleware(app)

        # Mock request with single-word Authorization
        request = Mock(spec=Request)
        request.url.path = "/api/test"
        request.headers.get = Mock(return_value="Bearer")

        call_next = AsyncMock()

        response = await middleware.dispatch(request, call_next)

        # Should return 401
        assert isinstance(response, JSONResponse)
        assert response.status_code == 401


class TestUserContextMiddleware:
    """Tests for UserContextMiddleware."""

    async def test_middleware_extracts_user_context(self):
        """Test that middleware extracts user context from request.state.user."""
        from app.middleware.user_context_middleware import UserContextMiddleware, current_user_context

        app = Mock()
        middleware = UserContextMiddleware(app)

        # Mock request with user data
        request = Mock(spec=Request)
        request.state = Mock()
        request.state.user = {
            "sub": "user-123",
            "email": "test@example.com",
            "name": "Test User",
            "token": "jwt-token",
            "scopes": ["read", "write"],
        }

        # Mock call_next
        expected_response = Response()

        async def mock_call_next(req):
            # Verify context is set during request processing
            ctx = current_user_context.get()
            assert ctx is not None
            assert ctx["user_id"] == "user-123"
            assert ctx["email"] == "test@example.com"
            return expected_response

        response = await middleware.dispatch(request, mock_call_next)

        # Should pass through
        assert response == expected_response

        # Context should be cleared after request
        assert current_user_context.get() is None

    async def test_middleware_handles_missing_user(self):
        """Test that middleware handles missing user gracefully."""
        from app.middleware.user_context_middleware import UserContextMiddleware, current_user_context

        app = Mock()
        middleware = UserContextMiddleware(app)

        # Mock request without user data
        request = Mock(spec=Request)
        request.state = Mock(spec=[])  # No user attribute

        # Mock call_next
        expected_response = Response()
        call_next = AsyncMock(return_value=expected_response)

        response = await middleware.dispatch(request, call_next)

        # Should pass through with None context
        assert response == expected_response

        # Context should be None after request
        assert current_user_context.get() is None


class TestRequestContextBuilder:
    """Tests for OrchestratorRequestContextBuilder."""

    async def test_build_with_user_context(self):
        """Test building request context with user information."""
        from app.handlers import OrchestratorRequestContextBuilder
        from app.middleware.user_context_middleware import current_user_context

        builder = OrchestratorRequestContextBuilder()

        # Set user context
        user_context = {
            "user_id": "user-123",
            "email": "test@example.com",
            "name": "Test User",
            "token": "jwt-token",
            "scopes": ["read"],
        }
        current_user_context.set(user_context)

        # Build context (no params needed, just testing user context extraction)
        context = await builder.build(context_id="ctx-123")

        # Verify user_id is set
        assert context.call_context.state["user_id"] == "user-123"
        assert context.call_context.state["user_email"] == "test@example.com"
        assert context.call_context.state["user_name"] == "Test User"

        # Cleanup
        current_user_context.set(None)

    async def test_build_without_user_context(self):
        """Test building request context without user information."""
        from app.handlers import OrchestratorRequestContextBuilder
        from app.middleware.user_context_middleware import current_user_context

        builder = OrchestratorRequestContextBuilder()

        # Ensure no user context
        current_user_context.set(None)

        # Build context
        context = await builder.build(context_id="ctx-123")

        # Should fall back to anonymous
        assert context.call_context.state["user_id"] == "anonymous"
