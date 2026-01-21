"""Tests for middleware components."""

from unittest.mock import AsyncMock, Mock, patch

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ringier_a2a_sdk.auth.jwt_validator import (
    ExpiredTokenError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
)
from ringier_a2a_sdk.middleware.orchestrator_jwt_middleware import (
    OrchestratorJWTMiddleware,
)
from ringier_a2a_sdk.middleware.user_context_middleware import (
    UserContextFromRequestStateMiddleware,
    current_user_context,
)


class TestOrchestratorJWTMiddleware:
    """Tests for OrchestratorJWTMiddleware."""

    def test_valid_token_passes(self, valid_jwt_token, rsa_key_pair):
        """Test that a valid JWT allows request through."""
        # public_key = rsa_key_pair["public_key"]

        # Mock validator
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(
            return_value={
                "iss": "https://login.example.com/realms/test",
                "sub": "service-account-orchestrator",
                "azp": "orchestrator",
                "aud": ["agent-1"],
                "exp": 9999999999,
                "iat": 1000000000,
            }
        )

        with patch("ringier_a2a_sdk.middleware.orchestrator_jwt_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                # Verify request.state has orchestrator info
                assert hasattr(request.state, "orchestrator")
                assert request.state.orchestrator["client_id"] == "orchestrator"
                assert "agent-1" in request.state.orchestrator["audiences"]
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 200
            assert response.text == "OK"

    def test_missing_token_returns_401(self):
        """Test that missing Authorization header returns 401."""

        def endpoint(request):
            return PlainTextResponse("OK")

        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[
                Middleware(
                    OrchestratorJWTMiddleware,
                    issuer="https://login.example.com/realms/test",
                    expected_azp="orchestrator",
                    expected_aud="agent-1",
                )
            ],
        )

        client = TestClient(app)
        response = client.post("/test")

        assert response.status_code == 401
        data = response.json()
        assert data.get("detail") or data.get("message")
        assert "Missing Authorization header" in (data.get("detail") or data.get("message"))

    def test_invalid_bearer_format_returns_401(self, valid_jwt_token):
        """Test that invalid Authorization format returns 401."""

        def endpoint(request):
            return PlainTextResponse("OK")

        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[
                Middleware(
                    OrchestratorJWTMiddleware,
                    issuer="https://login.example.com/realms/test",
                    expected_azp="orchestrator",
                    expected_aud="agent-1",
                )
            ],
        )

        client = TestClient(app)

        # Test without "Bearer " prefix
        response = client.post("/test", headers={"Authorization": valid_jwt_token})
        assert response.status_code == 401
        data = response.json()
        message = data.get("detail") or data.get("message")
        assert "Authorization header" in message and "Bearer" in message

    def test_expired_token_returns_401(self, expired_jwt_token):
        """Test that expired token returns 401."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=ExpiredTokenError("Token expired"))

        with patch("ringier_a2a_sdk.middleware.orchestrator_jwt_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {expired_jwt_token}"})

            assert response.status_code == 401
            data = response.json()
            message = data.get("detail") or data.get("message")
            assert "expired" in message.lower()

    def test_invalid_signature_returns_401(self, valid_jwt_token):
        """Test that invalid signature returns 401."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=InvalidSignatureError("Invalid signature"))

        with patch("ringier_a2a_sdk.middleware.orchestrator_jwt_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 401
            data = response.json()
            message = data.get("detail") or data.get("message")
            assert "signature" in message.lower()

    def test_invalid_issuer_returns_401(self, valid_jwt_token):
        """Test that invalid issuer returns 401."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=InvalidIssuerError("Invalid issuer"))

        with patch("ringier_a2a_sdk.middleware.orchestrator_jwt_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 401
            data = response.json()
            message = data.get("detail") or data.get("message")
            assert "issuer" in message.lower()

    def test_invalid_audience_returns_401(self, valid_jwt_token):
        """Test that invalid audience returns 401."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=InvalidAudienceError("Invalid audience"))

        with patch("ringier_a2a_sdk.middleware.orchestrator_jwt_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 401
            data = response.json()
            message = data.get("detail") or data.get("message")
            assert "audience" in message.lower()

    def test_public_paths_bypass_validation(self):
        """Test that paths in PUBLIC_PATHS bypass JWT validation."""

        def endpoint(request):
            return PlainTextResponse("OK")

        app = Starlette(
            routes=[
                Route("/health", endpoint),
                Route("/docs", endpoint),
                Route("/openapi.json", endpoint),
            ],
            middleware=[
                Middleware(
                    OrchestratorJWTMiddleware,
                    issuer="https://login.example.com/realms/test",
                    expected_azp="orchestrator",
                    expected_aud="agent-1",
                )
            ],
        )

        client = TestClient(app)

        # Test public paths without token
        for path in ["/health", "/docs", "/openapi.json"]:
            response = client.get(path)
            assert response.status_code == 200, f"Path {path} should be public"


class TestMiddlewareChain:
    """Tests for middleware chain execution order."""

    def test_middleware_chain_execution_order(self, valid_jwt_token, mock_a2a_request_body):
        """Test that OrchestratorJWTMiddleware runs before UserContextFromRequestStateMiddleware."""
        # Mock validator
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(
            return_value={
                "iss": "https://login.example.com/realms/test",
                "sub": "service-account-orchestrator",
                "azp": "orchestrator",
                "aud": ["agent-1"],
                "exp": 9999999999,
                "iat": 1000000000,
            }
        )

        with patch("ringier_a2a_sdk.middleware.orchestrator_jwt_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                # JWT middleware should have run and set orchestrator info
                assert hasattr(request.state, "orchestrator")
                assert request.state.orchestrator["client_id"] == "orchestrator"
                # Without upstream OIDC middleware, request.state.user won't be set
                # UserContextFromRequestStateMiddleware will set context to None
                ctx = current_user_context.get()
                assert ctx is None
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    # Middleware runs in reverse order (bottom-to-top for requests)
                    Middleware(UserContextFromRequestStateMiddleware),
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1",
                    ),
                ],
            )

            client = TestClient(app)
            response = client.post(
                "/test",
                json=mock_a2a_request_body,
                headers={
                    "Authorization": f"Bearer {valid_jwt_token}",
                    "Content-Type": "application/json",
                },
            )

            assert response.status_code == 200

    def test_jwt_failure_prevents_user_context_extraction(self, mock_a2a_request_body):
        """Test that JWT validation failure prevents user context middleware from running."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=InvalidSignatureError("Invalid signature"))

        with patch("ringier_a2a_sdk.middleware.orchestrator_jwt_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                # Should never reach here
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(UserContextFromRequestStateMiddleware),
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1",
                    ),
                ],
            )

            client = TestClient(app)
            response = client.post(
                "/test",
                json=mock_a2a_request_body,
                headers={"Authorization": "Bearer invalid-token", "Content-Type": "application/json"},
            )

            # Should fail at JWT validation
            assert response.status_code == 401
            data = response.json()
            message = data.get("detail") or data.get("message")
            assert "signature" in message.lower()


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
                    "sub": "user-123",
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
        assert context_during_request["user_context"]["user_id"] == "user-123"
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
