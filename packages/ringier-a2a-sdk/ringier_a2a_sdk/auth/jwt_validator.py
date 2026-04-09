"""
JWT validation and JWKS management for OIDC authentication.

This module provides utilities for validating JWT tokens issued by OIDC providers
using public keys fetched from the JWKS endpoint. It includes caching and
automatic discovery of OIDC endpoints.
"""

import logging
import os
from typing import Any, Dict, Optional

import aiohttp
import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

# Configuration
JWKS_CACHE_TTL_SECONDS = int(os.getenv("JWKS_CACHE_TTL_SECONDS", "3600"))


class JWTValidationError(Exception):
    """Base exception for JWT validation errors."""

    pass


class InvalidIssuerError(JWTValidationError):
    """Raised when the JWT issuer doesn't match the expected issuer."""

    pass


class InvalidAudienceError(JWTValidationError):
    """Raised when the JWT audience doesn't include the expected audience."""

    pass


class InvalidSignatureError(JWTValidationError):
    """Raised when the JWT signature validation fails."""

    pass


class ExpiredTokenError(JWTValidationError):
    """Raised when the JWT has expired."""

    pass


class MissingClaimError(JWTValidationError):
    """Raised when a required JWT claim is missing."""

    pass


class JWKSFetcher:
    """
    Fetches and caches JWKS (JSON Web Key Set) from OIDC provider.

    Automatically discovers the JWKS endpoint from the OIDC configuration
    and caches the keys for efficient validation.
    """

    def __init__(self, issuer: str, cache_ttl: int = JWKS_CACHE_TTL_SECONDS):
        """
        Initialize JWKS fetcher.

        Args:
            issuer: The OIDC issuer URL (e.g., https://login.nannos.ringier.ch/realms/nannos)
            cache_ttl: Cache TTL in seconds for JWKS keys
        """
        self.issuer = issuer.rstrip("/")
        self.cache_ttl = cache_ttl
        self._jwks_uri: Optional[str] = None
        self._jwk_client: Optional[PyJWKClient] = None

    async def _discover_jwks_uri(self) -> str:
        """
        Discover JWKS URI from OIDC configuration.

        Returns:
            The JWKS URI

        Raises:
            aiohttp.ClientError: If the discovery fails
            ValueError: If jwks_uri is not found in configuration
        """
        if self._jwks_uri:
            return self._jwks_uri

        well_known_url = f"{self.issuer}/.well-known/openid-configuration"
        logger.info(f"Discovering JWKS URI from: {well_known_url}")

        async with aiohttp.ClientSession() as session:
            async with session.get(well_known_url) as response:
                response.raise_for_status()
                metadata = await response.json()

        jwks_uri = metadata.get("jwks_uri")
        if not jwks_uri:
            raise ValueError(f"jwks_uri not found in OIDC configuration at {well_known_url}")

        self._jwks_uri = jwks_uri
        logger.info(f"Discovered JWKS URI: {jwks_uri}")
        return jwks_uri

    async def get_jwk_client(self) -> PyJWKClient:
        """
        Get or create PyJWKClient for fetching signing keys.

        Returns:
            Configured PyJWKClient instance
        """
        if self._jwk_client:
            return self._jwk_client

        jwks_uri = await self._discover_jwks_uri()

        # PyJWKClient handles caching automatically
        self._jwk_client = PyJWKClient(
            jwks_uri,
            cache_keys=True,
            lifespan=self.cache_ttl,
        )

        logger.info(f"Initialized PyJWKClient with cache TTL: {self.cache_ttl}s")
        return self._jwk_client


class JWTValidator:
    """
    Validates JWT tokens using JWKS public keys and verifies claims.

    This validator checks:
    - Signature validity using public keys from JWKS
    - Standard claims: iss, exp, nbf, iat
    - Custom claims: azp (authorized party), aud (audience)
    """

    def __init__(
        self,
        issuer: str,
        expected_azp: Optional[str] = None,
        expected_aud: Optional[str] = None,
        jwks_fetcher: Optional[JWKSFetcher] = None,
    ):
        """
        Initialize JWT validator.

        Args:
            issuer: Expected issuer URL
            expected_azp: Expected authorized party (client ID that requested the token)
            expected_aud: Expected audience (client ID that should accept the token)
            jwks_fetcher: Optional JWKSFetcher instance (creates new if not provided)
        """
        self.issuer = issuer.rstrip("/")
        self.expected_azp = expected_azp
        self.expected_aud = expected_aud
        self.jwks_fetcher = jwks_fetcher or JWKSFetcher(issuer)

    async def validate(self, token: str) -> Dict[str, Any]:
        """
        Validate JWT token and return decoded payload.

        Args:
            token: The JWT token string

        Returns:
            The decoded and validated JWT payload

        Raises:
            InvalidSignatureError: If signature validation fails
            ExpiredTokenError: If token has expired
            InvalidIssuerError: If issuer doesn't match
            InvalidAudienceError: If audience doesn't match
            MissingClaimError: If required claims are missing
        """
        try:
            # Get signing key from JWKS
            jwk_client = await self.jwks_fetcher.get_jwk_client()
            signing_key = jwk_client.get_signing_key_from_jwt(token)

            # Decode and verify token
            # Note: We disable automatic audience validation and do it manually below
            # to allow custom error messages and validation logic.
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": False,  # Disable automatic audience validation
                    "require": ["exp", "iat", "iss"],
                },
            )

            # Verify issuer
            token_issuer = payload.get("iss", "").rstrip("/")
            if token_issuer != self.issuer:
                raise InvalidIssuerError(f"Invalid issuer. Expected '{self.issuer}', got '{token_issuer}'")

            # Verify authorized party (azp) if expected
            if self.expected_azp:
                token_azp = payload.get("azp")
                if not token_azp:
                    raise MissingClaimError("Token missing 'azp' (authorized party) claim")
                if token_azp != self.expected_azp:
                    raise InvalidAudienceError(
                        f"Invalid authorized party. Expected '{self.expected_azp}', got '{token_azp}'"
                    )

            # Verify audience (aud) if expected
            if self.expected_aud:
                token_aud = payload.get("aud")
                if not token_aud:
                    raise MissingClaimError("Token missing 'aud' (audience) claim")

                # aud can be string or list
                audiences = [token_aud] if isinstance(token_aud, str) else token_aud
                if self.expected_aud not in audiences:
                    raise InvalidAudienceError(f"Invalid audience. Expected '{self.expected_aud}' in {audiences}")

            logger.debug(
                f"Successfully validated JWT: iss={token_issuer}, azp={payload.get('azp')}, aud={payload.get('aud')}"
            )

            return payload

        except jwt.ExpiredSignatureError as e:
            logger.warning(f"Token expired: {e}")
            raise ExpiredTokenError(f"Token has expired: {e}") from e

        except jwt.InvalidSignatureError as e:
            logger.warning(f"Invalid signature: {e}")
            raise InvalidSignatureError(f"Token signature validation failed: {e}") from e

        except jwt.DecodeError as e:
            logger.warning(f"Token decode error: {e}")
            raise InvalidSignatureError(f"Failed to decode token: {e}") from e

        except jwt.InvalidTokenError as e:
            # InvalidTokenError is the base class for various token errors
            # If we got here, it's likely a signature or decoding issue
            logger.warning(f"Invalid token: {e}")
            raise InvalidSignatureError(f"Token validation failed: {e}") from e


async def validate_orchestrator_jwt(
    token: str,
    issuer: str,
    orchestrator_client_id: str,
    agent_client_id: str,
) -> Dict[str, Any]:
    """
    Convenience function to validate orchestrator JWT with fail-fast semantics.

    Args:
        token: The JWT token string
        issuer: Expected OIDC issuer URL
        orchestrator_client_id: Expected orchestrator client ID (azp claim)
        agent_client_id: Expected agent client ID (aud claim)

    Returns:
        The validated JWT payload

    Raises:
        JWTValidationError: On any validation failure (fail-fast)
    """
    validator = JWTValidator(
        issuer=issuer,
        expected_azp=orchestrator_client_id,
        expected_aud=agent_client_id,
    )

    return await validator.validate(token)
