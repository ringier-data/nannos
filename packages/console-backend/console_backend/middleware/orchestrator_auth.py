"""Orchestrator authentication handler for httpx."""

import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

from ..config import config
from ..services.oauth_service import OAuthService
from ..utils.cookie_utils import (
    extract_cookie_attributes,
    find_cookie_in_headers,
    validate_cookie_domain,
)

if TYPE_CHECKING:
    from services.session_service import SessionService
    from utils.orchestrator_cookie_cache import OrchestratorCookieCache


logger = logging.getLogger(__name__)


class OrchestratorAuth(httpx.Auth):
    """httpx Auth handler that exchanges tokens for orchestrator requests.

    This auth handler integrates with httpx.AsyncClient to automatically
    exchange user tokens for orchestrator-specific tokens when making
    requests to the orchestrator agent. It also handles orchestrator session
    cookies which are stored in DynamoDB (with in-memory caching) to enable
    horizontal scaling across multiple backend servers.
    """

    def __init__(
        self,
        user_token: str,
        session_id: str,
        session_service: "SessionService",
        oauth_service: OAuthService,
        cookie_cache: "OrchestratorCookieCache",
        custom_headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize the auth handler.

        Args:
            user_token: The user's original access token to be exchanged
            session_id: The session ID (required for token refresh and cookie storage)
            session_service: The session service for token refresh
            oauth_service: The OAuth service for token operations
            cookie_cache: The orchestrator cookie cache for DynamoDB-backed storage
            custom_headers: Optional custom headers to forward to the orchestrator
        """
        self.user_access_token = user_token
        self.session_id = session_id
        self.session_service = session_service
        self.oauth_service = oauth_service
        self.cookie_cache = cookie_cache
        self.custom_headers = custom_headers or {}
        self.orchestrator_client_id = config.orchestrator.client_id

        # Determine schema based on orchestrator environment and base domain
        # Use https for remote domains, http only for localhost
        is_localhost = "localhost" in config.orchestrator.base_domain or "127.0.0.1" in config.orchestrator.base_domain
        schema = "http" if is_localhost else "https"

        self.orchestrator_base_url = f"{schema}://{config.orchestrator.base_domain}"
        self.orchestrator_base_domain = config.orchestrator.base_domain
        self._exchanged_token: str | None = None
        logger.info(f"OrchestratorAuth initialized with base URL: {self.orchestrator_base_url}")

    async def _get_orchestrator_token(self) -> str:
        """Get an orchestrator-specific token via token exchange.

        This method checks if the user's access token is expired and refreshes it
        if necessary before performing the token exchange.

        Returns:
            The exchanged token for the orchestrator

        Raises:
            TokenExchangeError: If token exchange fails
        """
        # Check if we need to refresh the user's access token
        # Get the session to check expiration time
        session = await self.session_service.get_session(self.session_id)
        if not session:
            logger.error(f"Session not found: {self.session_id}")
            raise Exception("Session not found")

        # Check if access token is expired or will expire soon (60 second buffer)
        now = datetime.now(timezone.utc)
        buffer_seconds = 60
        time_until_expiry = (session.access_token_expires_at - now).total_seconds()
        is_expired = time_until_expiry <= buffer_seconds

        logger.debug(
            f"Token expiry check: expires_at={session.access_token_expires_at.isoformat()}, "
            f"now={now.isoformat()}, time_until_expiry={time_until_expiry:.1f}s, "
            f"is_expired={is_expired}"
        )

        if is_expired:
            logger.info(
                f"User access token is expired or expiring soon (expires in {time_until_expiry:.1f}s), refreshing..."
            )
            try:
                # Refresh the access token
                refreshed_tokens = await self.oauth_service.refresh_token(session.refresh_token)

                # Update the user_access_token for future use
                self.user_access_token = refreshed_tokens["access_token"]

                # Calculate new expiration time
                expires_in = int(refreshed_tokens["expires_in"])
                new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                logger.info(
                    f"User access token refreshed, new expiry at {new_expires_at.isoformat()} "
                    f"({expires_in} seconds from now)"
                )

                # Update the session with new tokens
                await self.session_service.update_session(
                    session_id=self.session_id,
                    user_id=session.user_id,
                    access_token=refreshed_tokens["access_token"],
                    access_token_expires_at=new_expires_at,
                    refresh_token=refreshed_tokens.get("refresh_token", session.refresh_token),
                    id_token=refreshed_tokens.get("id_token", session.id_token),
                    issued_at=datetime.now(timezone.utc),
                )

                # Clear the cached exchanged token since we have a new access token
                self._exchanged_token = None

                # Clear orchestrator session cookie since it's tied to the old user token
                # The orchestrator will issue a new cookie when we send the new exchanged token
                await self.cookie_cache.clear_cookie(self.session_id)

                logger.info("Successfully refreshed user access token and cleared orchestrator cookie")

            except Exception as e:
                logger.error(f"Failed to refresh access token: {e}")

                # DO NOT destroy the HTTP session here!
                # The session is shared across all browser tabs for the same user.
                # Destroying it here would break all other tabs that are still using the session.
                # Instead, just raise the error and let the user re-login if needed.
                # The session will be cleaned up naturally via TTL or on explicit logout.

                # Raise a clear error so the caller can handle session expiration
                raise Exception("Session expired: Unable to refresh access token. Please re-authenticate.") from e

        if not self._exchanged_token:
            logger.debug(f"Exchanging token for orchestrator agent (client_id: {self.orchestrator_client_id})")
            self._exchanged_token = await self.oauth_service.exchange_token(
                subject_token=self.user_access_token,
                target_client_id=self.orchestrator_client_id,
                requested_scopes=["openid", "profile", "email"],
            )
            logger.debug("Token exchange successful")
        return self._exchanged_token

    def _is_orchestrator_request(self, url: httpx.URL) -> bool:
        """Check if the request is going to the orchestrator agent.

        Args:
            url: The request URL

        Returns:
            True if the request is for the orchestrator
        """
        logger.debug(f"Checking if URL is orchestrator request: {url}")
        if not self.orchestrator_base_url:
            return False
        if config.orchestrator.is_local():
            logger.debug("Local orchestrator detected - allowing localhost/0.0.0.0 requests")
            return str(url).startswith(self.orchestrator_base_url) or str(url).startswith("http://0.0.0.0")

        return str(url).startswith(self.orchestrator_base_url)

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Implement the httpx auth flow.

        Args:
            request: The httpx request to authenticate

        Yields:
            The authenticated request
        """
        logger.debug(f"Auth flow called for URL: {request.url}")
        logger.debug(f"Current headers: {dict(request.headers)}")

        # Check if this is an orchestrator request
        if self._is_orchestrator_request(request.url):
            logger.info(f"Orchestrator request detected: {request.url}")

            # Manual Cookie Management with DynamoDB Storage:
            # 1. First request: No cookie in DynamoDB yet, send bearer token
            # 2. Orchestrator validates token and returns Set-Cookie: orchestrator_session=JWT
            # 3. We extract cookie from Set-Cookie header and store in DynamoDB (with cache)
            # 4. Subsequent requests: Retrieve cookie from cache/DynamoDB and add Cookie header
            # 5. Orchestrator validates JWT cookie (no network call to OIDC provider!)
            # 6. If cookie is invalid/expired, orchestrator falls back to bearer token
            # 7. Orchestrator issues fresh cookie which we update in DynamoDB
            #
            # Note: We manage cookies manually (not using httpx's built-in cookie jar) because:
            # - httpx cookie jar is in-memory only and bound to the httpx.AsyncClient instance
            # - Our httpx clients are per-socket connection and destroyed on disconnect
            # - DynamoDB storage enables horizontal scaling (cookies shared across multiple backend servers)
            # - When load balancer routes requests to different servers, DynamoDB provides centralized cookie storage

            # Always send bearer token as fallback for expired cookies
            exchanged_token = await self._get_orchestrator_token()
            request.headers["Authorization"] = f"Bearer {exchanged_token}"
            logger.info(f"Sending request to orchestrator: {request.url}")

            # Add custom headers (e.g., X-Playground-SubAgentConfig-Hash for playground mode)
            for header_name, header_value in self.custom_headers.items():
                request.headers[header_name] = header_value
                logger.info(f"Added custom header: {header_name}={header_value}")

            # Try to get orchestrator session cookie from cache/DynamoDB
            cookie_data = await self.cookie_cache.get_cookie(self.session_id)
            if cookie_data:
                cookie, expires_at = cookie_data
                # Check if cookie is still valid
                now = datetime.now(timezone.utc)
                if expires_at > now:
                    request.headers["Cookie"] = cookie
                    logger.debug(f"Added orchestrator session cookie (expires: {expires_at.isoformat()})")
                else:
                    logger.debug("Orchestrator cookie expired, will rely on bearer token")
                    # Clear expired cookie from cache
                    self.cookie_cache.invalidate(self.session_id)
            else:
                logger.debug("No orchestrator session cookie yet (first request or cache miss)")
        else:
            logger.debug(f"Non-orchestrator request, skipping auth: {request.url}")

        response = yield request

        # Extract and store orchestrator session cookie from response
        if self._is_orchestrator_request(request.url):
            await self._extract_and_store_cookie(response)

    async def _extract_and_store_cookie(self, response: httpx.Response) -> None:
        """Extract orchestrator session cookie from Set-Cookie header and store in DynamoDB.

        Args:
            response: The httpx response containing Set-Cookie headers
        """
        set_cookie_headers = response.headers.get_list("set-cookie")

        if not set_cookie_headers:
            # This is normal for subsequent requests - orchestrator only sends cookie on first auth
            logger.debug("No new Set-Cookie headers (using cached cookie from previous auth)")
            return

        # Find orchestrator_session cookie in Set-Cookie headers
        cookie_header = find_cookie_in_headers(set_cookie_headers, "orchestrator_session")
        if not cookie_header:
            logger.debug("No orchestrator_session cookie in Set-Cookie headers")
            return

        # Extract cookie attributes using the utility
        try:
            attrs = extract_cookie_attributes(cookie_header, "orchestrator_session")
        except ValueError as e:
            logger.warning(f"Failed to parse orchestrator cookie: {e}")
            return

        # Validate cookie domain matches orchestrator base domain
        if attrs["domain"] is not None and not validate_cookie_domain(attrs["domain"], self.orchestrator_base_domain):
            logger.warning(
                f"Cookie domain mismatch: expected {self.orchestrator_base_domain}, "
                f"got {attrs['domain']}. Rejecting cookie."
            )
            return

        # Use calculated expiration from utility, or default to 15 minutes
        expires_at = attrs["expires_at"]
        if expires_at is None:
            default_expiry_minutes = 15
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=default_expiry_minutes)
            logger.debug(f"No Max-Age found, using default expiry: {default_expiry_minutes} minutes")

        # Store cookie in cache + DynamoDB
        try:
            await self.cookie_cache.set_cookie(self.session_id, attrs["value"], expires_at)
            logger.info(f"Stored orchestrator session cookie (expires: {expires_at.isoformat()})")
        except Exception as e:
            logger.error(f"Failed to store orchestrator cookie: {e}", exc_info=True)

    async def aclose(self) -> None:
        """Close and cleanup any resources.

        This is a no-op since OrchestratorAuth doesn't manage any resources
        that need cleanup. The method exists to satisfy cleanup expectations
        in ConnectionPool.
        """
        # No resources to clean up
        logger.debug("OrchestratorAuth.aclose() called - no cleanup needed")
