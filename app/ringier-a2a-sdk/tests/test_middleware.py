"""Tests for middleware components."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ringier_a2a_sdk.middleware.user_context_middleware import (
    UserContextFromRequestStateMiddleware,
    current_user_context,
)


class TestMiddlewareChain:
    """Tests for middleware chain execution order."""

    def test_middleware_handles_missing_user(self):
        """Test that middleware handles missing user gracefully."""

        def endpoint(request):
            # Context should be None
            ctx = current_user_context.get()
            assert ctx is None
            return PlainTextResponse("OK")

        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[Middleware(UserContextFromRequestStateMiddleware)],
        )

        client = TestClient(app)
        response = client.post("/test")

        # Should pass through with None context
        assert response.status_code == 200


class TestUserContextFromRequestStateMiddleware:
    """Tests for UserContextFromRequestStateMiddleware."""

    def test_middleware_extracts_user_context(self):
        """Test that middleware extracts user context from request.state.user."""

        # Track whether context was set during request processing
        context_during_request = {}

        def endpoint(request):
            # Capture context during request processing
            ctx = current_user_context.get()
            context_during_request["user_context"] = ctx
            return PlainTextResponse("OK")

        # Create mock upstream middleware that sets request.state.user
        class MockOidcMiddleware(Middleware):
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                from starlette.requests import Request

                request = Request(scope, receive)
                request.state.user = {
                    "sub": "sub-123",
                    "email": "test@example.com",
                    "name": "Test User",
                    "token": "jwt-token",
                    "scopes": ["read", "write"],
                }
                scope["state"] = request.state._state
                await self.app(scope, receive, send)

        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
        )

        # Manually wrap with our middlewares
        app = UserContextFromRequestStateMiddleware(app)
        app = MockOidcMiddleware(app)

        client = TestClient(app)
        response = client.post("/test")

        # Should pass through
        assert response.status_code == 200

        # Verify context was set during request processing
        assert context_during_request["user_context"] is not None
        assert context_during_request["user_context"]["user_sub"] == "sub-123"
        assert context_during_request["user_context"]["email"] == "test@example.com"

        # Context should be cleared after request
        assert current_user_context.get() is None

    def test_middleware_handles_missing_user(self):
        """Test that middleware handles missing user gracefully."""

        def endpoint(request):
            # Context should be None
            ctx = current_user_context.get()
            assert ctx is None
            return PlainTextResponse("OK")

        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[Middleware(UserContextFromRequestStateMiddleware)],
        )

        client = TestClient(app)
        response = client.post("/test")

        # Should pass through with None context
        assert response.status_code == 200

        # Context should be None after request
        assert current_user_context.get() is None
