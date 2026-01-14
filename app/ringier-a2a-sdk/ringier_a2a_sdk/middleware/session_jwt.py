"""
Session JWT utilities for OIDC userinfo caching.

This module provides utilities for creating and validating session JWTs used for
session management with the UserinfoMiddleware. These JWTs cache user
information from OIDC validation to avoid making userinfo API calls
on every request.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

logger = logging.getLogger(__name__)


# JWT Configuration
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = int(os.getenv("JWT_SESSION_EXPIRY_MINUTES", "15"))


def create_session_jwt(userinfo: dict, secret_key: str, expiry_minutes: int = 15) -> str:
    """
    Create a session JWT from OIDC userinfo.

    Args:
        userinfo: User information from OIDC userinfo endpoint
        secret_key: Secret key for signing the JWT
        expiry_minutes: Session expiry in minutes (default: 15)

    Returns:
        Signed JWT token containing user session information
    """
    # Validate required fields
    if not userinfo.get("sub"):
        raise ValueError("userinfo must contain 'sub' field")

    # Create JWT payload with standard claims
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=expiry_minutes)
    payload = {
        # Standard JWT claims
        "iss": "agent-session",
        "sub": userinfo.get("sub"),
        "iat": now,
        "exp": expiry,
        # User information from OIDC
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "preferred_username": userinfo.get("preferred_username"),
        "scopes": userinfo.get("scope", "").split(),
        "session_type": "oidc_cached",
    }

    # Create and sign the JWT
    token = jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)

    logger.debug(f"Created session JWT for user: {userinfo.get('sub')}")
    return token


def verify_session_jwt(token: str, secret_key: str) -> Optional[dict]:
    """Verify and decode a session JWT.

    Args:
        token: The JWT string to verify
        secret_key: Secret key for verifying the JWT signature

    Returns:
        The decoded JWT payload if valid, None if invalid or expired
    """
    try:
        # Verify signature and decode
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[JWT_ALGORITHM],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
            },
        )

        logger.debug(f"Verified session JWT for user: {payload.get('sub')}")
        return payload

    except jwt.ExpiredSignatureError:
        logger.debug("Session JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"Invalid session JWT: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error verifying JWT: {e}", exc_info=True)
        return None


def extract_userinfo_from_jwt(payload: dict) -> dict:
    """Extract user information from a verified JWT payload.

    Args:
        payload: The decoded JWT payload

    Returns:
        User information dictionary

    Note:
        Returns user information in the same format as returned
        by the OIDC userinfo endpoint
    """
    return {
        "sub": payload.get("sub"),
        "email": payload.get("email"),
        "name": payload.get("name"),
    }
