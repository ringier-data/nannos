"""
Pytest configuration and fixtures for ringier-a2a-sdk tests.
"""

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def rsa_key_pair():
    """Generate RSA key pair for testing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())

    public_key = private_key.public_key()

    # Export keys
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    return {
        "private_key": private_key,
        "public_key": public_key,
        "private_pem": private_pem,
        "public_pem": public_pem,
    }


@pytest.fixture
def mock_jwks_response(rsa_key_pair):
    """Generate mock JWKS response."""
    # Convert public key to JWK format
    public_key = rsa_key_pair["public_key"]
    public_numbers = public_key.public_numbers()

    # Base64url encode the numbers
    import base64

    def int_to_base64url(num):
        num_bytes = num.to_bytes((num.bit_length() + 7) // 8, byteorder="big")
        return base64.urlsafe_b64encode(num_bytes).rstrip(b"=").decode("utf-8")

    jwk = {
        "kty": "RSA",
        "use": "sig",
        "kid": "test-key-id",
        "alg": "RS256",
        "n": int_to_base64url(public_numbers.n),
        "e": int_to_base64url(public_numbers.e),
    }

    return {"keys": [jwk]}


@pytest.fixture
def mock_oidc_discovery():
    """Mock OIDC discovery response."""
    return {
        "issuer": "https://login.example.com/realms/test",
        "authorization_endpoint": "https://login.example.com/realms/test/protocol/openid-connect/auth",
        "token_endpoint": "https://login.example.com/realms/test/protocol/openid-connect/token",
        "jwks_uri": "https://login.example.com/realms/test/protocol/openid-connect/certs",
        "userinfo_endpoint": "https://login.example.com/realms/test/protocol/openid-connect/userinfo",
        "grant_types_supported": ["client_credentials", "authorization_code"],
        "response_types_supported": ["code", "token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


@pytest.fixture
def generate_test_jwt(rsa_key_pair):
    """Factory fixture to generate test JWTs with custom claims."""

    def _generate(
        claims: dict = None,
        expired: bool = False,
        invalid_signature: bool = False,
        private_key=None,
        issuer: str = None,
        azp: str = None,
        audience=None,
    ) -> str:
        """
        Generate a test JWT token.

        Args:
            claims: JWT claims to include (optional, can use keyword args instead)
            expired: Whether to generate an expired token
            invalid_signature: Whether to use wrong key for signature
            private_key: Private key to use (optional, uses fixture key by default)
            issuer: Issuer claim (optional)
            azp: Authorized party claim (optional)
            audience: Audience claim (optional, can be string or list)

        Returns:
            JWT token string
        """
        now = datetime.now(timezone.utc)

        default_claims = {
            "iss": issuer or "https://login.p.nannos.rcplus.io/realms/nannos",
            "sub": "service-account-orchestrator",
            "azp": azp or "orchestrator",
            "aud": audience or ["foundry-jira-ticket-agent"],
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5) if not expired else now - timedelta(minutes=1),
        }

        # Merge with provided claims if any
        token_claims = {**default_claims, **(claims or {})}

        # Choose key
        if invalid_signature:
            # Generate a different key for invalid signature
            wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
            key = wrong_key
        else:
            key = private_key or rsa_key_pair["private_key"]

        # Encode JWT
        token = pyjwt.encode(token_claims, key, algorithm="RS256", headers={"kid": "test-key-id"})

        return token

    return _generate


@pytest.fixture
def valid_jwt_token(generate_test_jwt):
    """Generate a valid JWT token for testing."""
    # Use issuer that matches test expectations
    return generate_test_jwt(issuer="https://login.example.com/realms/test", azp="orchestrator", audience=["agent-1"])


@pytest.fixture
def expired_jwt_token(generate_test_jwt):
    """Generate an expired JWT token for testing."""
    return generate_test_jwt({}, expired=True)


@pytest.fixture
def mock_a2a_request_body():
    """Generate mock A2A JSON-RPC request body with user context."""
    return {
        "jsonrpc": "2.0",
        "method": "agent/sendMessage",
        "id": "test-request-id",
        "params": {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "Create a Jira ticket"}]},
            "metadata": {"user_context": {"user_sub": "user-123", "email": "test@example.com", "name": "Test User"}},
        },
    }
