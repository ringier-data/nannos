"""
Smart Token Interceptor for A2A Agent-to-Agent Communication.

Automatically detects authentication requirements from AgentCard security configuration
and performs either OAuth2 token exchange (RFC 8693) or client credentials flow.

Features:
- Auto-detection: Examines AgentCard.security_schemes to determine auth requirements
- Token exchange: Exchanges user token for service-specific tokens via RFC 8693 (OIDC)
- Client credentials: Uses orchestrator JWT for bearer token authentication (JWT)
- No auth support: Skips authentication for public endpoints
- Token caching: Relies on OidcOAuth2Client's per-audience token caching with expiry checks
- User context propagation: Injects user context into message metadata for JWT auth
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

from a2a.client.middleware import ClientCallContext, ClientCallInterceptor
from a2a.types import AgentCard, HTTPAuthSecurityScheme

if TYPE_CHECKING:
    from ringier_a2a_sdk.oauth.client import OidcOAuth2Client


logger = logging.getLogger(__name__)


class SmartTokenInterceptor(ClientCallInterceptor):
    """
    Intelligent token interceptor that automatically determines auth strategy.

    This interceptor examines the target AgentCard's security configuration
    to automatically determine whether:
    1. No authentication is needed (public endpoint) - No auth header added
    2. JWT bearer authentication (orchestrator client credentials) - Uses get_token()
    3. OAuth2 token exchange (user token exchange) - Uses exchange_token() via RFC 8693

    If authentication is required but fails, the request proceeds without
    authentication (and will likely fail at the target agent).

    Usage:
        from ringier_a2a_sdk.oauth.client import OidcOAuth2Client

        oauth_client = OidcOAuth2Client(...)
        interceptor = SmartTokenInterceptor(
            user_token=user_jwt,
            oauth_client=oauth_client
        )
        config = A2AClientConfig(auth_interceptor=interceptor)
    """

    SUPPORTED_ISSUERS = [
        "https://login.alloy.ch/realms/a2a",
    ]

    def __init__(
        self,
        user_token: str,
        oauth2_client: "OidcOAuth2Client",
        user_context: Optional[dict[str, Any]] = None,
    ):
        """
        Initialize smart token interceptor.

        Args:
            user_token: User's authenticated access token
            user_context: Optional user context dict with user_id, email, name
            oauth_client: Optional OidcOAuth2Client for both JWT auth and token exchange
        """
        self.user_token = user_token
        self.user_context = user_context or {}
        self.oauth2_client = oauth2_client

    def _detect_auth_scheme(self, agent_card: AgentCard) -> tuple[str, str, Any]:
        """
        Detect authentication scheme from agent card.

        Returns:
            Tuple of (auth_type, scheme_name, scheme_object)
            where auth_type is "jwt" or "oidc"

        Raises:
            ValueError: If no supported scheme found
        """
        for scheme_name, scheme in (agent_card.security_schemes or {}).items():
            # Check for JWT bearer authentication
            if scheme.root.type == "http":
                http_scheme = scheme.root
                if isinstance(http_scheme, HTTPAuthSecurityScheme):
                    if http_scheme.scheme == "bearer" and http_scheme.bearer_format == "JWT":
                        return ("jwt", scheme_name, http_scheme)

            # Check for OpenID Connect
            if scheme.root.type == "openIdConnect":
                return ("oidc", scheme_name, scheme.root)

        raise ValueError(f"Agent {agent_card.name} does not have a supported security scheme (JWT or OIDC).")

    async def intercept(
        self,
        method_name: str,
        request_payload: dict[str, Any],
        http_kwargs: dict[str, Any],
        agent_card: AgentCard | None,
        context: ClientCallContext | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Intelligently add authentication based on agent card security config.

        Process:
        1. Check if agent_card has security configured
        2. Detect auth scheme (JWT bearer or OIDC)
        3. JWT: Use client credentials flow + inject user context into metadata
        4. OIDC: Use token exchange via RFC 8693
        5. If authentication fails: don't add auth header (request will likely fail)

        Args:
            method_name: A2A RPC method
            request_payload: JSON-RPC request payload
            http_kwargs: httpx request kwargs
            agent_card: Target agent metadata (examined for security config)
            context: Request context

        Returns:
            Tuple of (modified_request_payload, modified_http_kwargs)
        """
        # Ensure headers dict exists
        if "headers" not in http_kwargs:
            http_kwargs["headers"] = {}

        # No agent card means we can't determine auth requirements
        if not agent_card:
            logger.warning("No AgentCard provided, headers won't include auth")
            return request_payload, http_kwargs

        # No security schemes means no authentication required
        if agent_card.security_schemes is None or len(agent_card.security_schemes) == 0:
            logger.info(f"Agent {agent_card.name} has no security schemes, sending request without authentication.")
            return request_payload, http_kwargs

        # Detect authentication scheme
        try:
            auth_type, scheme_name, scheme_obj = self._detect_auth_scheme(agent_card)
        except ValueError as e:
            logger.warning(
                f"{e} "
                f"Available schemes: {list((agent_card.security_schemes or {}).keys())}. "
                "Proceeding without auth header."
            )
            return request_payload, http_kwargs

        # Handle JWT bearer authentication (client credentials)
        if auth_type == "jwt":
            return await self._handle_jwt_auth(agent_card, scheme_name, request_payload, http_kwargs)

        # Handle OIDC token exchange
        elif auth_type == "oidc":
            return await self._handle_oidc_auth(agent_card, scheme_name, scheme_obj, request_payload, http_kwargs)

        return request_payload, http_kwargs

    def _inject_user_context(self, request_payload: dict[str, Any]) -> None:
        """
        Inject user context into A2A message metadata.

        Modifies request_payload in-place to add user_context to metadata.
        Only includes attribution data (user_id, email, name).
        The orchestrator's JWT in the Authorization header is used for authentication.
        """
        if not self.user_context:
            return

        # Ensure params and metadata exist
        if "params" not in request_payload:
            request_payload["params"] = {}
        if "metadata" not in request_payload["params"]:
            request_payload["params"]["metadata"] = {}

        # Inject user context for attribution only (no access token)
        request_payload["params"]["metadata"]["user_context"] = {
            "user_id": self.user_context.get("user_id"),
            "email": self.user_context.get("email"),
            "name": self.user_context.get("name"),
        }

        logger.debug(f"Injected user context into message metadata: user_id={self.user_context.get('user_id')}")

    async def _handle_jwt_auth(
        self,
        agent_card: AgentCard,
        scheme_name: str,
        request_payload: dict[str, Any],
        http_kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Handle JWT bearer authentication using client credentials.

        Args:
            agent_card: Target agent card
            scheme_name: Security scheme name (used as audience)
            request_payload: JSON-RPC request payload
            http_kwargs: httpx request kwargs

        Returns:
            Tuple of (modified_request_payload, modified_http_kwargs)
        """
        try:
            # Get token for target agent (scheme_name is the agent's client ID)
            target_client_id = scheme_name
            token = await self.oauth2_client.get_token(audience=target_client_id)

            # Add to headers
            http_kwargs["headers"]["Authorization"] = f"Bearer {token}"

            # Inject user context into message metadata
            self._inject_user_context(request_payload)

            logger.info(f"Successfully obtained client credentials token for {agent_card.name}")

        except Exception as e:
            logger.error(
                f"Client credentials auth failed for {agent_card.name}: {e}. "
                "Request will be sent without authentication and will likely fail."
            )

        return request_payload, http_kwargs

    async def _handle_oidc_auth(
        self,
        agent_card: AgentCard,
        scheme_name: str,
        scheme_obj: Any,
        request_payload: dict[str, Any],
        http_kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Handle OIDC authentication using token exchange.

        Args:
            agent_card: Target agent card
            scheme_name: Security scheme name
            scheme_obj: OpenID Connect scheme object
            request_payload: JSON-RPC request payload
            http_kwargs: httpx request kwargs

        Returns:
            Tuple of (request_payload, modified_http_kwargs)
        """
        # Verify supported issuer
        for issuer in self.SUPPORTED_ISSUERS:
            if scheme_obj.open_id_connect_url.startswith(issuer):
                break
        else:
            logger.warning(
                f"Agent {agent_card.name} uses unsupported OIDC issuer: {scheme_obj.open_id_connect_url}. "
                "Proceeding without auth header."
            )
            return request_payload, http_kwargs

        # Extract required scopes
        required_scopes = []
        for security in agent_card.security or []:
            if scheme_name in security:
                required_scopes.extend(security[scheme_name])
                break

        # Perform token exchange
        try:
            target_client_id = scheme_name
            exchanged_token = await self.oauth2_client.exchange_token(
                subject_token=self.user_token,
                target_client_id=target_client_id,
                requested_scopes=required_scopes if required_scopes else None,
            )

            # Add to headers
            http_kwargs["headers"]["Authorization"] = f"Bearer {exchanged_token}"

            logger.info(f"Successfully exchanged token for {agent_card.name}")

        except Exception as e:
            logger.error(
                f"Token exchange failed for {agent_card.name}: {e}. "
                "Request will be sent without authentication and will likely fail."
            )

        return request_payload, http_kwargs

    def clear_cache(self):
        """Clear the token cache in the OAuth2 client."""
        self.oauth2_client.clear_cache()
