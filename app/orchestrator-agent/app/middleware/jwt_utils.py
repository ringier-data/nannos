"""
JWT utilities for creating and verifying session tokens.

This module provides functions to create and verify signed JWTs used for
session management. These JWTs cache user information from Okta OIDC
validation to avoid making userinfo API calls on every request.
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
JWT_SECRET_KEY: str = os.environ["JWT_SECRET_KEY"]


def create_session_jwt(userinfo: dict) -> str:
    """Create a signed JWT containing user session information.
    
    Args:
        userinfo: User information from Okta userinfo endpoint
        
    Returns:
        A signed JWT string
        
    Raises:
        ValueError: If required fields are missing from userinfo
    """
    # Validate required fields
    if not userinfo.get("sub"):
        raise ValueError("userinfo must contain 'sub' field")
    
    # Create JWT payload with standard claims
    now = datetime.now(timezone.utc)
    payload = {
        # Standard JWT claims
        "iat": now,  # Issued at
        "exp": now + timedelta(minutes=JWT_EXPIRY_MINUTES),  # Expiration
        "nbf": now,  # Not before
        
        # User information from Okta
        "sub": userinfo.get("sub"),
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        
        # Session metadata
        "session_type": "okta_cached",
    }
    
    # Create and sign the JWT
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    
    logger.debug(f"Created session JWT for user: {userinfo.get('sub')}")
    return token


def verify_session_jwt(token: str) -> Optional[dict]:
    """Verify and decode a session JWT.
    
    Args:
        token: The JWT string to verify
        
    Returns:
        The decoded JWT payload if valid, None if invalid or expired
    """
    try:
        # Verify signature and decode
        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
            }
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
        A dictionary containing user information in the same format
        as returned by the Okta userinfo endpoint
    """
    return {
        "sub": payload.get("sub"),
        "email": payload.get("email"),
        "name": payload.get("name"),
    }
