"""Shared Gatana MCP gateway authentication utilities."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..config import config
from ..models.user import User

logger = logging.getLogger(__name__)


async def exchange_for_audience(subject_token: str, target_client_id: str) -> str:
    """Exchange a user-scoped token for one the named audience will accept.

    Every audience validates its own: the MCP gateway rejects a token minted for this
    backend and vice versa, so a call has to be paired with the right exchange. Callable
    without a Request on purpose — the scheduler acts on behalf of a job's owner, where
    there is no request at all.

    Raises:
        ValueError: the exchange was refused.
    """
    oauth2_client = OidcOAuth2Client(
        client_id=config.oidc.client_id,
        client_secret=config.oidc.client_secret.get_secret_value(),
        issuer=config.oidc.issuer,
    )
    try:
        return await oauth2_client.exchange_token(
            subject_token=subject_token,
            target_client_id=target_client_id,
            requested_scopes=["openid", "profile", "offline_access"],
        )
    except Exception as exc:
        raise ValueError(f"Failed to exchange token for the {target_client_id} audience: {exc}") from exc


async def exchange_for_gatana(subject_token: str) -> str:
    """Exchange a user-scoped token for one the MCP gateway will accept."""
    return await exchange_for_audience(subject_token, config.mcp_gateway.client_id)


async def get_user_subject_token(request: Request, user: User) -> str:
    """The token that identifies the user, whichever way they authenticated.

    Two patterns reach this backend, and both end with a token that still has to be
    exchanged for whatever audience is about to be called:
    1. Session-based (frontend): the user token from request.state, refreshed if needed.
    2. Bearer (orchestrator/A2A): the incoming token, minted for the agent-console
       audience to reach us.

    Split from the exchange on purpose: which audience to ask for depends on which server
    hosts the tool being called, and only the caller knows that.

    Args:
        request: FastAPI request object
        user: Authenticated user

    Returns:
        A user-scoped token, not yet exchanged for any target audience.

    Raises:
        HTTPException: If the token is missing or cannot be refreshed.
    """
    # Check if Authorization header is present (Bearer token from orchestrator)
    auth_header = request.headers.get("Authorization")

    if auth_header:
        # Bearer token path (orchestrator/A2A service calls).
        #
        # The caller authenticates to console-backend with an `agent-console`-audience
        # token (the orchestrator exchanges user → CONSOLE_BACKEND_CLIENT_ID to reach us,
        # see orchestrator discovery), NOT a token for whatever we are about to call.
        # Returned unexchanged on purpose: this function's job is to identify the user,
        # and the exchange belongs to the caller that knows the target audience —
        # `get_gatana_token` for the gateway, `token_for` when the audience follows the
        # tool. Both paths of this function return a subject token, and every caller
        # exchanges it; a gateway call made with this token directly would be a 401.
        if not auth_header.startswith("Bearer "):
            logger.error("Invalid Authorization header format")
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
            )
        incoming_token = auth_header[len("Bearer ") :].strip()

        return incoming_token

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

    return access_token


async def get_gatana_token(request: Request, user: User) -> str:
    """The MCP gateway token for this request's user.

    Gatana validates the audience and rejects a token minted for any other client, so
    every path to the gateway ends here.

    Raises:
        HTTPException: If the token is missing, expired, or the exchange fails.
    """
    subject_token = await get_user_subject_token(request, user)
    try:
        token = await exchange_for_gatana(subject_token)
        logger.info(f"Exchanged token for {config.mcp_gateway.client_id} audience (user {user.email})")
        return token
    except ValueError as e:
        logger.error(f"Token exchange failed: {e}")
        raise HTTPException(status_code=401, detail=f"Token exchange failed: {e}")
