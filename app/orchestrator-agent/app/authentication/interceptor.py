"""
Smart Token Interceptor for A2A Agent-to-Agent Communication.

Automatically detects authentication requirements from AgentCard security configuration
and performs OAuth2 token exchange (RFC 8693) when needed.

Features:
- Auto-detection: Examines AgentCard.security_schemes to determine auth requirements
- Token exchange: Exchanges user token for service-specific tokens via RFC 8693
- No auth support: Skips authentication for public endpoints
- Token caching: Caches exchanged tokens per agent to minimize API calls
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

from a2a.client.middleware import ClientCallContext, ClientCallInterceptor
from a2a.types import AgentCard, OpenIdConnectSecurityScheme

if TYPE_CHECKING:
    from .okta_token_exchange import OktaTokenExchanger


logger = logging.getLogger(__name__)


class SmartTokenInterceptor(ClientCallInterceptor):
    """
    Intelligent token interceptor that automatically determines auth strategy.

    This interceptor examines the target AgentCard's security configuration
    to automatically determine whether:
    1. No authentication is needed (public endpoint) - No auth header added
    2. OAuth2 token exchange is required - Performs RFC 8693 token exchange

    If token exchange is required but fails, the request proceeds without
    authentication (and will likely fail at the target agent).

    Usage:
        from .okta_token_exchange import OktaTokenExchanger

        token_exchanger = OktaTokenExchanger(...)
        interceptor = SmartTokenInterceptor(
            user_token=user_jwt,
            token_exchanger=token_exchanger
        )
        config = A2AClientConfig(auth_interceptor=interceptor)
    """

    SUPPORTED_ISSUERS = [
        "https://login.alloy.ch/realms/a2a",
    ]

    def __init__(
        self,
        user_token: str,
        token_exchanger: Optional["OktaTokenExchanger"] = None,  # type: ignore
    ):
        """
        Initialize smart token interceptor.

        Args:
            user_token: User's authenticated access token
            token_exchanger: Optional OktaTokenExchanger for token exchange
                           Required if calling agents with OAuth2 security
        """
        self.user_token = user_token
        self.token_exchanger = token_exchanger

        # Cache of agent_name -> exchanged_token to avoid repeated exchanges
        self._token_cache: dict[str, str] = {}

    def _get_openidconnect_scheme(self, agent_card: AgentCard) -> tuple[str, OpenIdConnectSecurityScheme]:
        """Retrieve the OpenID Connect security scheme from the agent card, if present."""
        for scheme_name, scheme in (agent_card.security_schemes or {}).items():
            if scheme.root.type == "openIdConnect":
                return scheme_name, scheme.root

        raise ValueError(f"Agent {agent_card.name} does not have an OpenID Connect security scheme.")

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
        1. Check if agent_card has OAuth2 security configured
        2. If no: don't add auth header (public endpoint)
        3. If yes: perform token exchange via RFC 8693
        4. If token exchange fails: don't add auth header (request will likely fail)

        Args:
            method_name: A2A RPC method
            request_payload: JSON-RPC request payload
            http_kwargs: httpx request kwargs
            agent_card: Target agent metadata (examined for security config)
            context: Request context

        Returns:
            Tuple of (request_payload, modified_http_kwargs)
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

        # Check cache first
        if agent_card.name in self._token_cache:
            logger.debug(f"Using cached token for {agent_card.name}")
            http_kwargs["headers"]["Authorization"] = f"Bearer {self._token_cache[agent_card.name]}"
            return request_payload, http_kwargs

        # Determine if OpenID Connect security is configured (only OIDC supported for now)
        try:
            scheme_name, opendid_scheme = self._get_openidconnect_scheme(agent_card)
        except ValueError:
            logger.warning(
                f"Agent {agent_card.name} does not specify OpenID Connect security scheme. "
                f"Following schemes are present: {list((agent_card.security_schemes or {}).keys())}. "
                "Proceeding without auth header."
            )
            return request_payload, http_kwargs

        # Verify supported issuer
        for issuer in self.SUPPORTED_ISSUERS:
            if opendid_scheme.open_id_connect_url.startswith(issuer):
                break
        else:
            logger.warning(
                f"Agent {agent_card.name} uses unsupported OIDC issuer: {opendid_scheme.open_id_connect_url}. "
                "Proceeding without auth header."
            )
            return request_payload, http_kwargs

        # Ensure token exchanger is provided
        if not self.token_exchanger:
            logger.warning(
                "Token exchanger not provided. Cannot perform token exchange for OpenID Connect secured agents. "
                "Proceeding without auth header."
            )
            return request_payload, http_kwargs

        # NOTE: according to https://swagger.io/specification/#security-requirement-object if openIdConnect is used,
        # the scopes are defined per security requirement object
        required_scopes = []
        for security in agent_card.security or []:
            if scheme_name in security:
                required_scopes.extend(security[scheme_name])
                break

        # Perform token exchange
        try:
            target_client_id = scheme_name
            exchanged_token = await self.token_exchanger.exchange_token(
                subject_token=self.user_token,
                target_client_id=target_client_id,
                requested_scopes=required_scopes if required_scopes else None,
            )

            # Cache the exchanged token
            self._token_cache[agent_card.name] = exchanged_token

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
        """Clear the token cache to force new exchanges."""
        self._token_cache.clear()
