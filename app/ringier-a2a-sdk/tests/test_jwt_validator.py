"""Tests for JWT validation components."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import jwt
import pytest
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from ringier_a2a_sdk.auth.jwt_validator import (
    ExpiredTokenError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    JWKSFetcher,
    JWTValidator,
    MissingClaimError,
)


class TestJWKSFetcher:
    """Tests for JWKSFetcher class."""

    @pytest.mark.asyncio
    async def test_discover_jwks_uri_success(self, mock_oidc_discovery):
        """Test successful OIDC discovery."""
        fetcher = JWKSFetcher(issuer="https://login.example.com/realms/test")
        
        with patch("aiohttp.ClientSession") as mock_session_class:
            mock_session = AsyncMock()
            mock_response = AsyncMock()
            mock_response.raise_for_status = AsyncMock()
            mock_response.json = AsyncMock(return_value=mock_oidc_discovery)
            mock_session.get = Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))
            mock_session_class.return_value = AsyncMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock())
            
            jwks_uri = await fetcher._discover_jwks_uri()

        assert jwks_uri == "https://login.example.com/realms/test/protocol/openid-connect/certs"

    @pytest.mark.asyncio
    async def test_discover_jwks_uri_missing_field(self):
        """Test OIDC discovery with missing jwks_uri field."""
        fetcher = JWKSFetcher(issuer="https://login.example.com/realms/test")
        
        mock_response_data = {"issuer": "https://login.example.com/realms/test"}
        
        with patch("aiohttp.ClientSession") as mock_session_class:
            mock_session = AsyncMock()
            mock_response = AsyncMock()
            mock_response.raise_for_status = AsyncMock()
            mock_response.json = AsyncMock(return_value=mock_response_data)
            mock_session.get = Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))
            mock_session_class.return_value = AsyncMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock())
            
            with pytest.raises(ValueError, match="jwks_uri not found in OIDC configuration"):
                await fetcher._discover_jwks_uri()

    @pytest.mark.asyncio
    async def test_get_jwk_client_caching(self, mock_oidc_discovery):
        """Test that PyJWKClient is created with caching enabled."""
        fetcher = JWKSFetcher(
            issuer="https://login.example.com/realms/test",
            cache_ttl=7200
        )
        
        with patch("aiohttp.ClientSession") as mock_session_class:
            mock_session = AsyncMock()
            mock_response = AsyncMock()
            mock_response.raise_for_status = AsyncMock()
            mock_response.json = AsyncMock(return_value=mock_oidc_discovery)
            mock_session.get = Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))
            mock_session_class.return_value = AsyncMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock())

            with patch("ringier_a2a_sdk.auth.jwt_validator.PyJWKClient") as mock_jwk_client_class:
                mock_jwk_client = Mock()
                mock_jwk_client_class.return_value = mock_jwk_client

                client = await fetcher.get_jwk_client()
                
                assert client == mock_jwk_client
                mock_jwk_client_class.assert_called_once_with(
                    "https://login.example.com/realms/test/protocol/openid-connect/certs",
                    cache_keys=True,
                    lifespan=7200
                )

    @pytest.mark.asyncio
    async def test_get_jwk_client_default_ttl(self, mock_oidc_discovery):
        """Test default JWKS cache TTL."""
        fetcher = JWKSFetcher(issuer="https://login.example.com/realms/test")
        
        with patch("aiohttp.ClientSession") as mock_session_class:
            mock_session = AsyncMock()
            mock_response = AsyncMock()
            mock_response.raise_for_status = AsyncMock()
            mock_response.json = AsyncMock(return_value=mock_oidc_discovery)
            mock_session.get = Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))
            mock_session_class.return_value = AsyncMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock())

            with patch("ringier_a2a_sdk.auth.jwt_validator.PyJWKClient") as mock_jwk_client_class:
                await fetcher.get_jwk_client()                # Check that default TTL (3600) is used
                _, kwargs = mock_jwk_client_class.call_args
                assert kwargs["lifespan"] == 3600


class TestJWTValidator:
    """Tests for JWTValidator class."""

    @pytest.mark.asyncio
    async def test_validate_success(self, rsa_key_pair, valid_jwt_token, mock_jwks_response):
        """Test successful JWT validation."""
        # private_key = rsa_key_pair["private_key"]
        public_key = rsa_key_pair["public_key"]
        
        mock_jwk_client = Mock()
        mock_signing_key = Mock()
        mock_signing_key.key = public_key
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key
        
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        validator = JWTValidator(
            issuer="https://login.example.com/realms/test",
            expected_azp="orchestrator",
            expected_aud="agent-1",
            jwks_fetcher=mock_fetcher
        )
        
        result = await validator.validate(valid_jwt_token)
        
        assert result["iss"] == "https://login.example.com/realms/test"
        assert result["azp"] == "orchestrator"
        assert result["aud"] == ["agent-1"]
        assert "exp" in result
        assert "iat" in result

    @pytest.mark.asyncio
    async def test_validate_expired_token(self, expired_jwt_token, mock_jwks_response):
        """Test validation of expired token."""
        mock_jwk_client = Mock()
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        # Mock PyJWT to raise ExpiredSignatureError
        with patch("jwt.decode", side_effect=ExpiredSignatureError("Token expired")):
            validator = JWTValidator(
                issuer="https://login.example.com/realms/test",
                expected_azp="orchestrator",
                expected_aud="agent-1",
                jwks_fetcher=mock_fetcher
            )
            
            with pytest.raises(ExpiredTokenError, match="Token has expired"):
                await validator.validate(expired_jwt_token)

    @pytest.mark.asyncio
    async def test_validate_invalid_signature(self, rsa_key_pair, valid_jwt_token):
        """Test validation with invalid signature."""
        public_key = rsa_key_pair["public_key"]
        
        mock_jwk_client = Mock()
        mock_signing_key = Mock()
        mock_signing_key.key = public_key
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key
        
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        # Mock PyJWT to raise InvalidTokenError for signature verification
        with patch("ringier_a2a_sdk.auth.jwt_validator.jwt.decode", side_effect=InvalidTokenError("Signature verification failed")):
            validator = JWTValidator(
                issuer="https://login.example.com/realms/test",
                expected_azp="orchestrator",
                expected_aud="agent-1",
                jwks_fetcher=mock_fetcher
            )

            with pytest.raises(InvalidSignatureError):
                await validator.validate(valid_jwt_token)

    @pytest.mark.asyncio
    async def test_validate_invalid_issuer(self, rsa_key_pair, generate_test_jwt, mock_jwks_response):
        """Test validation with wrong issuer."""
        private_key = rsa_key_pair["private_key"]
        public_key = rsa_key_pair["public_key"]
        
        # Create token with wrong issuer
        token = generate_test_jwt(
            private_key=private_key,
            issuer="https://wrong-issuer.com",
            azp="orchestrator",
            audience=["agent-1"]
        )
        
        mock_jwk_client = Mock()
        mock_signing_key = Mock()
        mock_signing_key.key = public_key
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key
        
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        validator = JWTValidator(
            issuer="https://login.example.com/realms/test",
            expected_azp="orchestrator",
            expected_aud="agent-1",
            jwks_fetcher=mock_fetcher
        )
        
        with pytest.raises(InvalidIssuerError, match="Invalid issuer"):
            await validator.validate(token)

    @pytest.mark.asyncio
    async def test_validate_invalid_azp(self, rsa_key_pair, generate_test_jwt, mock_jwks_response):
        """Test validation with wrong azp (authorized party)."""
        private_key = rsa_key_pair["private_key"]
        public_key = rsa_key_pair["public_key"]
        
        # Create token with wrong azp
        token = generate_test_jwt(
            private_key=private_key,
            issuer="https://login.example.com/realms/test",
            azp="wrong-client",
            audience=["agent-1"]
        )
        
        mock_jwk_client = Mock()
        mock_signing_key = Mock()
        mock_signing_key.key = public_key
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key
        
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        validator = JWTValidator(
            issuer="https://login.example.com/realms/test",
            expected_azp="orchestrator",
            expected_aud="agent-1",
            jwks_fetcher=mock_fetcher
        )
        
        with pytest.raises(InvalidAudienceError, match="Invalid authorized party"):
            await validator.validate(token)

    @pytest.mark.asyncio
    async def test_validate_invalid_audience(self, rsa_key_pair, generate_test_jwt, mock_jwks_response):
        """Test validation with wrong audience."""
        private_key = rsa_key_pair["private_key"]
        public_key = rsa_key_pair["public_key"]
        
        # Create token with wrong audience
        token = generate_test_jwt(
            private_key=private_key,
            issuer="https://login.example.com/realms/test",
            azp="orchestrator",
            audience=["wrong-agent"]
        )
        
        mock_jwk_client = Mock()
        mock_signing_key = Mock()
        mock_signing_key.key = public_key
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key
        
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        validator = JWTValidator(
            issuer="https://login.example.com/realms/test",
            expected_azp="orchestrator",
            expected_aud="agent-1",
            jwks_fetcher=mock_fetcher
        )
        
        with pytest.raises(InvalidAudienceError, match="Invalid audience"):
            await validator.validate(token)

    @pytest.mark.asyncio
    async def test_validate_missing_required_claim(self, rsa_key_pair, mock_jwks_response):
        """Test validation with missing required claim."""
        private_key = rsa_key_pair["private_key"]
        public_key = rsa_key_pair["public_key"]
        
        # Create token without 'azp' claim
        now = datetime.now(timezone.utc)
        payload = {
            "iss": "https://login.example.com/realms/test",
            "sub": "service-account-orchestrator",
            "aud": ["agent-1"],
            "exp": now + timedelta(hours=1),
            "iat": now,
            "nbf": now
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")
        
        mock_jwk_client = Mock()
        mock_signing_key = Mock()
        mock_signing_key.key = public_key
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key
        
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        validator = JWTValidator(
            issuer="https://login.example.com/realms/test",
            expected_azp="orchestrator",
            expected_aud="agent-1",
            jwks_fetcher=mock_fetcher
        )
        
        with pytest.raises(MissingClaimError, match="missing 'azp'"):
            await validator.validate(token)

    @pytest.mark.asyncio
    async def test_validate_audience_as_string(self, rsa_key_pair, generate_test_jwt, mock_jwks_response):
        """Test validation when audience is a string instead of list."""
        private_key = rsa_key_pair["private_key"]
        public_key = rsa_key_pair["public_key"]
        
        # Create token with audience as string
        now = datetime.now(timezone.utc)
        payload = {
            "iss": "https://login.example.com/realms/test",
            "sub": "service-account-orchestrator",
            "azp": "orchestrator",
            "aud": "agent-1",  # String instead of list
            "exp": now + timedelta(hours=1),
            "iat": now,
            "nbf": now
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")
        
        mock_jwk_client = Mock()
        mock_signing_key = Mock()
        mock_signing_key.key = public_key
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key
        
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        validator = JWTValidator(
            issuer="https://login.example.com/realms/test",
            expected_azp="orchestrator",
            expected_aud="agent-1",
            jwks_fetcher=mock_fetcher
        )
        
        result = await validator.validate(token)
        # Validator should handle string audience correctly
        assert "aud" in result

    @pytest.mark.asyncio
    async def test_validate_multiple_audiences(self, rsa_key_pair, generate_test_jwt, mock_jwks_response):
        """Test validation with multiple audiences."""
        private_key = rsa_key_pair["private_key"]
        public_key = rsa_key_pair["public_key"]
        
        # Create token with multiple audiences
        token = generate_test_jwt(
            private_key=private_key,
            issuer="https://login.example.com/realms/test",
            azp="orchestrator",
            audience=["agent-1", "agent-2", "agent-3"]
        )
        
        mock_jwk_client = Mock()
        mock_signing_key = Mock()
        mock_signing_key.key = public_key
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key
        
        mock_fetcher = Mock()
        mock_fetcher.get_jwk_client = AsyncMock(return_value=mock_jwk_client)
        
        validator = JWTValidator(
            issuer="https://login.example.com/realms/test",
            expected_azp="orchestrator",
            expected_aud="agent-2",  # Should match one of the audiences
            jwks_fetcher=mock_fetcher
        )
        
        result = await validator.validate(token)
        assert "agent-2" in result["aud"]
        assert len(result["aud"]) == 3
