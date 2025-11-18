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
    UserContextFromMetadataMiddleware,
)


class TestOrchestratorJWTMiddleware:
    """Tests for OrchestratorJWTMiddleware."""

    def test_valid_token_passes(self, valid_jwt_token, rsa_key_pair):
        """Test that a valid JWT allows request through."""
        # public_key = rsa_key_pair["public_key"]
        
        # Mock validator
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(return_value={
            "iss": "https://login.example.com/realms/test",
            "sub": "service-account-orchestrator",
            "azp": "orchestrator",
            "aud": ["agent-1"],
            "exp": 9999999999,
            "iat": 1000000000
        })
        
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
                        expected_aud="agent-1"
                    )
                ]
            )
            
            client = TestClient(app)
            response = client.post(
                "/test",
                headers={"Authorization": f"Bearer {valid_jwt_token}"}
            )
            
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
                    expected_aud="agent-1"
                )
            ]
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
                    expected_aud="agent-1"
                )
            ]
        )
        
        client = TestClient(app)
        
        # Test without "Bearer " prefix
        response = client.post(
            "/test",
            headers={"Authorization": valid_jwt_token}
        )
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
                        expected_aud="agent-1"
                    )
                ]
            )
            
            client = TestClient(app)
            response = client.post(
                "/test",
                headers={"Authorization": f"Bearer {expired_jwt_token}"}
            )
            
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
                        expected_aud="agent-1"
                    )
                ]
            )
            
            client = TestClient(app)
            response = client.post(
                "/test",
                headers={"Authorization": f"Bearer {valid_jwt_token}"}
            )
            
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
                        expected_aud="agent-1"
                    )
                ]
            )
            
            client = TestClient(app)
            response = client.post(
                "/test",
                headers={"Authorization": f"Bearer {valid_jwt_token}"}
            )
            
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
                        expected_aud="agent-1"
                    )
                ]
            )
            
            client = TestClient(app)
            response = client.post(
                "/test",
                headers={"Authorization": f"Bearer {valid_jwt_token}"}
            )
            
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
                    expected_aud="agent-1"
                )
            ]
        )
        
        client = TestClient(app)
        
        # Test public paths without token
        for path in ["/health", "/docs", "/openapi.json"]:
            response = client.get(path)
            assert response.status_code == 200, f"Path {path} should be public"


class TestUserContextFromMetadataMiddleware:
    """Tests for UserContextFromMetadataMiddleware."""

    def test_extracts_user_context_from_metadata(self, mock_a2a_request_body):
        """Test that user context is extracted from A2A message metadata."""
        def endpoint(request):
            # Verify user context was extracted
            assert hasattr(request.state, "user")
            assert request.state.user["user_id"] == "user-123"
            assert request.state.user["email"] == "test@example.com"
            assert request.state.user["name"] == "Test User"
            return PlainTextResponse("OK")
        
        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[Middleware(UserContextFromMetadataMiddleware)]
        )
        
        client = TestClient(app)
        response = client.post(
            "/test",
            json=mock_a2a_request_body,
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 200

    def test_missing_user_context_continues_processing(self):
        """Test that missing user context logs warning but allows request through."""
        def endpoint(request):
            # Should still reach endpoint even without user context
            assert not hasattr(request.state, "user") or request.state.user is None
            return PlainTextResponse("OK")
        
        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[Middleware(UserContextFromMetadataMiddleware)]
        )
        
        client = TestClient(app)
        
        # Request without user_context in metadata
        body = {
            "jsonrpc": "2.0",
            "method": "test_method",
            "params": {
                "metadata": {}  # No user_context
            },
            "id": 1
        }
        
        response = client.post(
            "/test",
            json=body,
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 200

    def test_missing_metadata_continues_processing(self):
        """Test that missing metadata allows request through."""
        def endpoint(request):
            return PlainTextResponse("OK")
        
        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[Middleware(UserContextFromMetadataMiddleware)]
        )
        
        client = TestClient(app)
        
        # Request without metadata
        body = {
            "jsonrpc": "2.0",
            "method": "test_method",
            "params": {},  # No metadata
            "id": 1
        }
        
        response = client.post(
            "/test",
            json=body,
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 200

    def test_malformed_json_continues_processing(self):
        """Test that malformed JSON allows request through (logged but not blocked)."""
        def endpoint(request):
            return PlainTextResponse("OK")
        
        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[Middleware(UserContextFromMetadataMiddleware)]
        )
        
        client = TestClient(app)
        
        # Non-JSON content
        response = client.post(
            "/test",
            content="not json",
            headers={"Content-Type": "text/plain"}
        )
        
        # Middleware should not block non-JSON requests
        assert response.status_code == 200

    def test_partial_user_context_extracted(self):
        """Test that partial user context (missing some fields) is still extracted."""
        def endpoint(request):
            assert hasattr(request.state, "user")
            assert request.state.user["user_id"] == "user-123"
            # email and name might be missing but that's ok
            return PlainTextResponse("OK")
        
        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[Middleware(UserContextFromMetadataMiddleware)]
        )
        
        client = TestClient(app)
        
        # Request with partial user context
        body = {
            "jsonrpc": "2.0",
            "method": "test_method",
            "params": {
                "metadata": {
                    "user_context": {
                        "user_id": "user-123"
                        # Missing email and name
                    }
                }
            },
            "id": 1
        }
        
        response = client.post(
            "/test",
            json=body,
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 200


class TestMiddlewareChain:
    """Tests for middleware chain execution order."""

    def test_middleware_chain_execution_order(self, valid_jwt_token, mock_a2a_request_body):
        """Test that OrchestratorJWTMiddleware runs before UserContextFromMetadataMiddleware."""
        # Mock validator
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(return_value={
            "iss": "https://login.example.com/realms/test",
            "sub": "service-account-orchestrator",
            "azp": "orchestrator",
            "aud": ["agent-1"],
            "exp": 9999999999,
            "iat": 1000000000
        })
        
        with patch("ringier_a2a_sdk.middleware.orchestrator_jwt_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator
            
            def endpoint(request):
                # Both middlewares should have run
                assert hasattr(request.state, "orchestrator")
                assert hasattr(request.state, "user")
                assert request.state.orchestrator["client_id"] == "orchestrator"
                assert request.state.user["user_id"] == "user-123"
                return PlainTextResponse("OK")
            
            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    # Middleware runs in reverse order (bottom-to-top for requests)
                    Middleware(UserContextFromMetadataMiddleware),
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1"
                    )
                ]
            )
            
            client = TestClient(app)
            response = client.post(
                "/test",
                json=mock_a2a_request_body,
                headers={
                    "Authorization": f"Bearer {valid_jwt_token}",
                    "Content-Type": "application/json"
                }
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
                    Middleware(UserContextFromMetadataMiddleware),
                    Middleware(
                        OrchestratorJWTMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1"
                    )
                ]
            )
            
            client = TestClient(app)
            response = client.post(
                "/test",
                json=mock_a2a_request_body,
                headers={
                    "Authorization": "Bearer invalid-token",
                    "Content-Type": "application/json"
                }
            )
            
            # Should fail at JWT validation
            assert response.status_code == 401
            data = response.json()
            message = data.get("detail") or data.get("message")
            assert "signature" in message.lower()
