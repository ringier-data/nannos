"""
Smart Token Interceptor for A2A Agent-to-Agent Communication.

Automatically detects authentication requirements from AgentCard security configuration
and exchanges user tokens for target-specific tokens before passing to sub-agents.

Features:
- Auto-detection: Examines AgentCard.security_schemes to determine auth requirements
- Token exchange: Always exchanges user token for target-specific token (orchestrator or agent-creator)
- Scope reduction: Limits scopes to [openid, profile, email] to remove broader user permissions
- Audience scoping: Tokens are targeted for specific services (orchestrator vs agent-creator)
- Dynamic provisioning: No per-agent client registration needed
- User context propagation: User context preserved in JWT claims (sub, email, name, groups)

Token Exchange Strategy:
1. User token (from client) → Orchestrator validates
2. Orchestrator exchanges for target-specific token:
   - Default: audience=orchestrator with scopes [openid, profile, email]
   - agent-creator: audience=agent-creator with scopes [openid, profile, email]
3. Orchestrator passes exchanged token to sub-agents
4. Sub-agents validate token locally via JWTValidatorMiddleware

Security Considerations:
- ✅ Scope reduction: Removes broader scopes the user might have (e.g., playground access)
- ✅ Audience scoping: Token is for specific service (orchestrator/agent-creator), not arbitrary services
- ⚠️  Lateral movement: Compromised sub-agent CAN still call orchestrator (token has aud=orchestrator)
  and invoke other agents on behalf of the user. User's groups/permissions remain in token.
- ⚠️  MCP gateway access: Sub-agents can still exchange tokens for MCP gateway access if needed
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

from a2a.client.middleware import ClientCallContext, ClientCallInterceptor
from a2a.types import AgentCard

if TYPE_CHECKING:
    from ringier_a2a_sdk.oauth.client import OidcOAuth2Client


logger = logging.getLogger(__name__)


class SmartTokenInterceptor(ClientCallInterceptor):
    """
    Intelligent token interceptor that automatically determines auth strategy.

    This interceptor examines the target AgentCard's security configuration
    to automatically determine whether:
    1. No authentication is needed (public endpoint) - No auth header added
    2. OAuth2 token exchange (OIDC) - Always exchanges user token for target-specific token

    Token Exchange Targets:
    - Default: 'orchestrator' target with reduced scopes (openid, profile, email)
    - Exception: 'agent-creator' uses its own client ID to preserve playground access

    This provides:
    - Scope reduction: Tokens have minimal scopes [openid, profile, email] instead of user's full scopes
    - Audience scoping: Tokens targeted for orchestrator or agent-creator (not arbitrary services)
    - Dynamic provisioning: No per-agent client registration needed
    - Selective access: agent-creator gets playground access, others get orchestrator-scoped tokens

    Security Note:
    - Compromised sub-agent CAN still call orchestrator with the token (aud=orchestrator)
    - User permissions (groups) remain in token, so orchestrator will honor them
    - This is NOT defense against lateral movement, but DOES limit token scope

    If authentication is required but fails, the request proceeds without
    authentication (and will likely fail at the target agent).

    Usage:
        from ringier_a2a_sdk.oauth.client import OidcOAuth2Client

        oauth_client = OidcOAuth2Client(...)
        interceptor = SmartTokenInterceptor(
            user_token=user_jwt,
            oauth_client=oauth_client,
        )
        config = A2AClientConfig(auth_interceptor=interceptor)
    """

    def __init__(
        self,
        user_token: str,
        oauth2_client: "OidcOAuth2Client",
        user_context: Optional[dict[str, Any]] = None,
        sub_agent_id: Optional[int] = None,
    ):
        """
        Initialize smart token interceptor.

        Args:
            user_token: User's authenticated access token
            oauth2_client: OAuth2 client for token operations
            user_context: Optional user context dict with user_id, email, name
            sub_agent_id: Optional sub-agent ID for cost tracking attribution
        """
        self.user_token = user_token
        self.user_context = user_context or {}
        self.oauth2_client = oauth2_client
        self.sub_agent_id = sub_agent_id
        self._exchanged_tokens: dict[str, str] = {}  # Cache: target_client_id -> token
        self.oidc_issuer = self.oauth2_client.issuer

    def _detect_auth_scheme(self, agent_card: AgentCard) -> tuple[str, str, Any]:
        """
        Detect authentication scheme from agent card.

        Returns:
            Tuple of (auth_type, scheme_name, scheme_object)
            where auth_type is "oidc"

        Raises:
            ValueError: If no supported scheme found
        """
        for scheme_name, scheme in (agent_card.security_schemes or {}).items():
            # Check for OpenID Connect
            if scheme.root.type == "openIdConnect":
                return ("oidc", scheme_name, scheme.root)

        raise ValueError(f"Agent {agent_card.name} does not have a supported security scheme (OIDC).")

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
        2. Detect auth scheme (OIDC)
        3. OIDC: Exchange user token for target-specific token and pass to sub-agent
        4. If authentication fails: don't add auth header (request will likely fail)

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

        # Handle OIDC authentication (pass user token directly)
        if auth_type == "oidc":
            return await self._handle_oidc_auth(agent_card, scheme_name, scheme_obj, request_payload, http_kwargs)

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
        Handle OIDC authentication by exchanging user token for target-specific token.

        Token exchange targets:
        - agent-creator: Uses 'agent-creator' target to preserve playground endpoint access
        - All other agents: Uses 'orchestrator' target with reduced scopes

        This provides scope-based isolation while maintaining dynamic provisioning.

        Args:
            agent_card: Target agent card
            scheme_name: Security scheme name (agent's client ID)
            scheme_obj: OpenID Connect scheme object
            request_payload: JSON-RPC request payload
            http_kwargs: httpx request kwargs

        Returns:
            Tuple of (request_payload, modified_http_kwargs)
        """
        # Verify issuer matches configuration
        if not scheme_obj.open_id_connect_url.startswith(self.oidc_issuer):
            logger.warning(
                f"Agent {agent_card.name} uses different OIDC issuer: {scheme_obj.open_id_connect_url} "
                f"(expected: {self.oidc_issuer}). Proceeding without auth header."
            )
            return request_payload, http_kwargs

        # Determine target client ID based on agent requirements
        # agent-creator needs playground access, others use reduced-scope orchestrator token
        if scheme_name == "agent-creator":
            # NOTE: the agent-creator client is provisioned manually to allow playground access
            target_client_id = "agent-creator"
            requested_scopes = ["openid", "profile", "email"]  # Preserve playground access
            token_description = "agent-creator token (playground access)"
        else:
            # NOTE: we could also decide to have a shared sub-agents client with reduced scopes
            target_client_id = "orchestrator"
            requested_scopes = ["openid", "profile", "email"]  # Reduced scopes
            token_description = "orchestrator token (reduced scopes)"

        # Exchange user token for target-specific token (with caching)
        try:
            if target_client_id not in self._exchanged_tokens:
                logger.info(f"Exchanging user token for {target_client_id} token")
                self._exchanged_tokens[target_client_id] = await self.oauth2_client.exchange_token(
                    subject_token=self.user_token,
                    target_client_id=target_client_id,
                    requested_scopes=requested_scopes,
                )
                logger.info(f"Token exchange successful for target={target_client_id}")

            exchanged_token = self._exchanged_tokens[target_client_id]

            # Add token to headers
            http_kwargs["headers"]["Authorization"] = f"Bearer {exchanged_token}"

            # Add sub_agent_id as HTTP header for cost tracking
            if self.sub_agent_id:
                http_kwargs["headers"]["X-Sub-Agent-Id"] = str(self.sub_agent_id)
                logger.debug(f"Added X-Sub-Agent-Id header: {self.sub_agent_id}")

            # Note: User context is already in token claims (sub, email, name, groups)
            # No need for additional headers

            logger.info(f"Successfully passing {token_description} to {agent_card.name}")

        except Exception as e:
            logger.error(
                f"Token exchange failed for {agent_card.name} (target={target_client_id}): {e}. "
                "Request will be sent without authentication and will likely fail."
            )

        return request_payload, http_kwargs

    def clear_cache(self):
        """Clear the token cache in the OAuth2 client."""
        self.oauth2_client.clear_cache()
