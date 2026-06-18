"""Shared Gatana MCP gateway authentication utilities."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..config import config
from ..models.user import User

logger = logging.getLogger(__name__)


async def get_gatana_token(request: Request, user: User) -> str:
    """Get Gatana MCP gateway token from request.

    Both authentication patterns end by exchanging a subject token for the Gatana
    (MCP gateway) audience — Gatana validates the audience and rejects tokens minted
    for any other client:
    1. Session-based (frontend): takes the user token from request.state, refreshes it
       if needed, then exchanges it for the Gatana audience.
    2. Bearer token (orchestrator/A2A): takes the incoming Bearer token (minted for the
       agent-console audience to reach console-backend) and exchanges it for the Gatana
       audience.

    Args:
        request: FastAPI request object
        user: Authenticated user

    Returns:
        Gatana MCP gateway token (already exchanged)

    Raises:
        HTTPException: If token is missing, expired, or exchange fails
    """
    # Check if Authorization header is present (Bearer token from orchestrator)
    auth_header = request.headers.get("Authorization")

    if auth_header:
        # Bearer token path (orchestrator/A2A service calls).
        #
        # The caller authenticates to console-backend with an `agent-console`-audience
        # token (the orchestrator exchanges user → CONSOLE_BACKEND_CLIENT_ID to reach us,
        # see orchestrator discovery), NOT a gatana token. Gatana validates the audience
        # and rejects an agent-console token with 401, so we must exchange the incoming
        # token for the gatana client id here — mirroring the session path below.
        if not auth_header.startswith("Bearer "):
            logger.error("Invalid Authorization header format")
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
            )
        incoming_token = auth_header[len("Bearer ") :].strip()

        oauth2_client = OidcOAuth2Client(
            client_id=config.oidc.client_id,
            client_secret=config.oidc.client_secret.get_secret_value(),
            issuer=config.oidc.issuer,
        )
        try:
            mcp_gateway_token = await oauth2_client.exchange_token(
                subject_token=incoming_token,
                target_client_id=config.mcp_gateway.client_id,
                requested_scopes=["openid", "profile", "offline_access"],
            )
            logger.info(
                f"Exchanged Bearer token for {config.mcp_gateway.client_id} audience "
                f"(user {user.email})"
            )
            return mcp_gateway_token
        except Exception as e:
            logger.error(f"Failed to exchange Bearer token for gatana audience: {e}")
            raise HTTPException(
                status_code=401,
                detail="Failed to exchange token for MCP gateway audience.",
            )

    # Session-based path: need to get user token and exchange it for Gatana token
    access_token = getattr(request.state, "access_token", None)
    if not access_token:
        logger.error(f"No access token available for user {user.email}")
        raise HTTPException(
            status_code=401,
            detail="No access token available. Please log in again.",
        )

    # Check if token is expired or expiring soon (within 60 seconds)
    token_expiry = getattr(request.state, "access_token_expires_at", None)
    if token_expiry:
        time_until_expiry = (token_expiry - datetime.now(timezone.utc)).total_seconds()
        is_expired = time_until_expiry < 60
    else:
        # If no expiry info, assume token might be expired
        is_expired = True
        time_until_expiry = 0

    if is_expired:
        logger.info(
            f"User access token is expired or expiring soon (expires in {time_until_expiry:.1f}s), refreshing..."
        )
        try:
            # Get refresh token from session
            refresh_token = getattr(request.state, "refresh_token", None)
            if not refresh_token:
                logger.error(f"No refresh token available for user {user.email}")
                raise HTTPException(
                    status_code=401,
                    detail="Session expired. Please log in again.",
                )

            # Create OAuth2 client for refresh
            oauth2_client = OidcOAuth2Client(
                client_id=config.oidc.client_id,
                client_secret=config.oidc.client_secret.get_secret_value(),
                issuer=config.oidc.issuer,
            )

            # Refresh the access token
            refreshed_tokens = await oauth2_client.refresh_token(refresh_token)

            # Update access token for current request
            access_token = refreshed_tokens["access_token"]

            # Calculate new expiration time
            expires_in = int(refreshed_tokens["expires_in"])
            new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            logger.info(
                f"User access token refreshed, new expiry at {new_expires_at.isoformat()} "
                f"({expires_in} seconds from now)"
            )

            logger.info(f"Successfully refreshed access token for user {user.email}")

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to refresh access token: {e}")
            raise HTTPException(
                status_code=401,
                detail="Session expired: Unable to refresh access token. Please re-authenticate.",
            )

    # Exchange user token for MCP gateway token
    oauth2_client = OidcOAuth2Client(
        client_id=config.oidc.client_id,
        client_secret=config.oidc.client_secret.get_secret_value(),
        issuer=config.oidc.issuer,
    )

    try:
        mcp_gateway_token = await oauth2_client.exchange_token(
            subject_token=access_token,
            target_client_id=config.mcp_gateway.client_id,
            requested_scopes=["openid", "profile", "offline_access"],
        )
        logger.info(f"Successfully exchanged token for {config.mcp_gateway.client_id} for user {user.email}")
        return mcp_gateway_token
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        raise HTTPException(
            status_code=401,
            detail=f"Token exchange failed: {str(e)}",
        )
