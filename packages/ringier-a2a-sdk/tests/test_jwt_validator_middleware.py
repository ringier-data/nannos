"""Tests for JWTValidatorMiddleware."""

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
    MissingClaimError,
)
from ringier_a2a_sdk.middleware.jwt_validator_middleware import JWTValidatorMiddleware


class TestJWTValidatorMiddleware:
    """Tests for JWTValidatorMiddleware."""

    def test_valid_token_passes(self, valid_jwt_token, rsa_key_pair):
        """Test that a valid JWT allows request through and sets request.state.user."""
        # Mock validator
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(
            return_value={
                "iss": "https://login.example.com/realms/test",
                "sub": "user-123",
                "email": "test@example.com",
                "name": "Test User",
                "azp": "agent-console",
                "aud": ["agent-1"],
                "exp": 9999999999,
                "iat": 1000000000,
                "groups": ["engineering", "admin"],
            }
        )

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                # Verify request.state has user info
                assert hasattr(request.state, "user")
                assert request.state.user["sub"] == "user-123"
                assert request.state.user["email"] == "test@example.com"
                assert request.state.user["name"] == "Test User"
                assert request.state.user["token"] == valid_jwt_token
                assert request.state.user["groups"] == ["engineering", "admin"]
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="agent-console",
                        expected_aud="agent-1",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 200
            assert response.text == "OK"

    def test_valid_token_without_azp_validation(self, valid_jwt_token):
        """Test that token passes when expected_azp is None (no azp validation)."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(
            return_value={
                "iss": "https://login.example.com/realms/test",
                "sub": "user-123",
                "email": "test@example.com",
                "name": "Test User",
                "exp": 9999999999,
                "iat": 1000000000,
            }
        )

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                assert hasattr(request.state, "user")
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp=None,  # No azp validation
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 200

    def test_missing_token_returns_401(self):
        """Test that missing Authorization header returns 401."""

        def endpoint(request):
            return PlainTextResponse("OK")

        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[
                Middleware(
                    JWTValidatorMiddleware,
                    issuer="https://login.example.com/realms/test",
                    expected_azp="orchestrator",
                )
            ],
        )

        client = TestClient(app)
        response = client.post("/test")

        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert data["error"] == "unauthorized"
        assert "Missing Authorization header" in data["message"]

    def test_invalid_bearer_format_returns_401(self, valid_jwt_token):
        """Test that invalid Authorization format returns 401."""

        def endpoint(request):
            return PlainTextResponse("OK")

        app = Starlette(
            routes=[Route("/test", endpoint, methods=["POST"])],
            middleware=[
                Middleware(
                    JWTValidatorMiddleware,
                    issuer="https://login.example.com/realms/test",
                    expected_azp="orchestrator",
                )
            ],
        )

        client = TestClient(app)

        # Test without "Bearer " prefix
        response = client.post("/test", headers={"Authorization": valid_jwt_token})
        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert data["error"] == "invalid_token_format"
        assert "Bearer" in data["message"]

    def test_expired_token_returns_401(self, expired_jwt_token):
        """Test that expired token returns 401."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=ExpiredTokenError("Token expired"))

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {expired_jwt_token}"})

            assert response.status_code == 401
            data = response.json()
            assert "error" in data
            assert data["error"] == "token_expired"
            assert "expired" in data["message"].lower()

    def test_invalid_signature_returns_401(self, valid_jwt_token):
        """Test that invalid signature returns 401."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=InvalidSignatureError("Invalid signature"))

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 401
            data = response.json()
            assert "error" in data
            assert data["error"] == "invalid_signature"
            assert "signature" in data["message"].lower()

    def test_invalid_issuer_returns_401(self, valid_jwt_token):
        """Test that invalid issuer returns 401."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=InvalidIssuerError("Invalid issuer"))

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 401
            data = response.json()
            assert "error" in data
            assert data["error"] == "invalid_issuer"
            assert "issuer" in data["message"].lower()

    def test_invalid_audience_returns_403(self, valid_jwt_token):
        """Test that invalid audience returns 403 (forbidden)."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=InvalidAudienceError("Invalid audience"))

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator",
                        expected_aud="agent-1",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 403  # Forbidden, not unauthorized
            data = response.json()
            assert "error" in data
            assert data["error"] == "invalid_audience"
            assert "audience" in data["message"].lower()

    def test_missing_claim_returns_401(self, valid_jwt_token):
        """Test that missing required claim returns 401."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(side_effect=MissingClaimError("Missing required claim: sub"))

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 401
            data = response.json()
            assert "error" in data
            assert "invalid_token" in data["error"]

    def test_public_paths_bypass_validation(self):
        """Test that paths in PUBLIC_PATHS bypass JWT validation."""

        def endpoint(request):
            return PlainTextResponse("OK")

        app = Starlette(
            routes=[
                Route("/.well-known/agent-card.json", endpoint),
                Route("/health", endpoint),
                Route("/docs", endpoint),
                Route("/openapi.json", endpoint),
            ],
            middleware=[
                Middleware(
                    JWTValidatorMiddleware,
                    issuer="https://login.example.com/realms/test",
                    expected_azp="orchestrator",
                )
            ],
        )

        client = TestClient(app)

        # Test public paths without token
        for path in ["/.well-known/agent-card.json", "/health", "/docs", "/openapi.json"]:
            response = client.get(path)
            assert response.status_code == 200, f"Path {path} should be public"

    def test_groups_claim_defaults_to_empty_list(self, valid_jwt_token):
        """Test that missing groups claim defaults to empty list."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(
            return_value={
                "iss": "https://login.example.com/realms/test",
                "sub": "user-123",
                "email": "test@example.com",
                "name": "Test User",
                # No groups claim
            }
        )

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                assert request.state.user["groups"] == []
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 200

    def test_orchestrator_token_with_azp_validation(self, valid_jwt_token):
        """Test validation with expected_azp for orchestrator tokens."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(
            return_value={
                "iss": "https://login.example.com/realms/test",
                "sub": "user-123",
                "azp": "orchestrator-client-id",
                "aud": ["agent-1"],
                "email": "test@example.com",
            }
        )

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                assert hasattr(request.state, "user")
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                        expected_azp="orchestrator-client-id",  # Require token from orchestrator
                        expected_aud="agent-1",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})

            assert response.status_code == 200
            # Verify validator was called with correct config
            mock_validator_class.assert_called_once()
            call_kwargs = mock_validator_class.call_args[1]
            assert call_kwargs["expected_azp"] == "orchestrator-client-id"
            assert call_kwargs["expected_aud"] == "agent-1"

    def test_phone_number_extracted_from_jwt(self, valid_jwt_token):
        """Test that phone_number claim is extracted from JWT."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(
            return_value={
                "sub": "user-123",
                "email": "test@example.com",
                "name": "Test User",
                "phone_number": "+41791234567",
                "exp": 9999999999,
                "iat": 1000000000,
            }
        )

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                assert request.state.user["phone_number"] == "+41791234567"
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})
            assert response.status_code == 200

    def test_phone_number_none_when_not_in_jwt(self, valid_jwt_token):
        """Test that phone_number is None when JWT doesn't contain the claim."""
        mock_validator = Mock()
        mock_validator.validate = AsyncMock(
            return_value={
                "sub": "user-123",
                "email": "test@example.com",
                "name": "Test User",
                "exp": 9999999999,
                "iat": 1000000000,
            }
        )

        with patch("ringier_a2a_sdk.middleware.jwt_validator_middleware.JWTValidator") as mock_validator_class:
            mock_validator_class.return_value = mock_validator

            def endpoint(request):
                assert request.state.user["phone_number"] is None
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[Route("/test", endpoint, methods=["POST"])],
                middleware=[
                    Middleware(
                        JWTValidatorMiddleware,
                        issuer="https://login.example.com/realms/test",
                    )
                ],
            )

            client = TestClient(app)
            response = client.post("/test", headers={"Authorization": f"Bearer {valid_jwt_token}"})
            assert response.status_code == 200
