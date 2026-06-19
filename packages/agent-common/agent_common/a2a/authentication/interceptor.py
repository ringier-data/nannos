"""Smart Token Interceptor for A2A Agent-to-Agent Communication.

Automatically detects authentication requirements from AgentCard security configuration
and exchanges user tokens for target-specific tokens before passing to sub-agents.

Features:
- Auto-detection: Examines AgentCard.security_schemes to determine auth requirements
- Token exchange: Always exchanges user token for target-specific token (orchestrator or a specific client)
- Scope reduction: Limits scopes to [openid, profile, email] to remove broader user permissions
- Audience scoping: Tokens are targeted for specific services (orchestrator vs a specific service)
- Dynamic provisioning: No per-agent client registration needed
- User context propagation: User context preserved in JWT claims (sub, email, name, groups)

Token Exchange Strategy:
1. User token (from client) → Orchestrator validates
2. Orchestrator exchanges for target-specific token:
   - Default: audience=orchestrator with scopes [openid, profile, email]
   - voice-agent: audience=voice-agent with scopes [openid, profile, email]
3. Orchestrator passes exchanged token to sub-agents
4. Sub-agents validate token locally via JWTValidatorMiddleware

Security Considerations:
- ✅ Scope reduction: Removes broader scopes the user might have (e.g., console access)
- ✅ Audience scoping: Token is for specific service (orchestrator/voice-agent), not arbitrary services
- ⚠️  Lateral movement: Compromised sub-agent CAN still call orchestrator (token has aud=orchestrator)
  and invoke other agents on behalf of the user. User's groups/permissions remain in token.
- ⚠️  MCP gateway access: Sub-agents can still exchange tokens for MCP gateway access if needed
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

from a2a.client.client import ClientCallContext
from a2a.client.interceptors import AfterArgs, BeforeArgs, ClientCallInterceptor
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
    - Exception: certain agents (e.g. 'voice-agent') use their own client ID

    This provides:
    - Scope reduction: Tokens have minimal scopes [openid, profile, email] instead of user's full scopes
    - Audience scoping: Tokens targeted for orchestrator or a specific service (not arbitrary services)
    - Dynamic provisioning: No per-agent client registration needed
    - Selective access: certain agents (e.g. voice-agent) use their own client; others get orchestrator-scoped tokens

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
        sub_agent_id: Optional[int] = None,
    ):
        """
        Initialize smart token interceptor.

        Args:
            user_token: User's authenticated access token
            oauth2_client: OAuth2 client for token operations
            sub_agent_id: Optional sub-agent ID for cost tracking attribution
        """
        self.user_token = user_token
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
            # A2A v1.0+: SecurityScheme is a protobuf oneof; OIDC is one variant.
            if scheme.HasField("open_id_connect_security_scheme"):
                return ("oidc", scheme_name, scheme.open_id_connect_security_scheme)

        raise ValueError(f"Agent {agent_card.name} does not have a supported security scheme (OIDC).")

    async def before(self, args: BeforeArgs) -> None:
        """
        Intelligently add authentication based on agent card security config.

        A2A v1.0+ interceptor contract: instead of returning modified payloads,
        we mutate ``args.context.service_parameters`` to inject request headers
        (the transport applies these to the outbound HTTP request).

        Process:
        1. Check if agent_card has security configured
        2. Detect auth scheme (OIDC)
        3. OIDC: Exchange user token for target-specific token and inject header
        4. If authentication fails: don't add auth header (request will likely fail)
        """
        agent_card = args.agent_card

        # No agent card means we can't determine auth requirements
        if not agent_card:
            logger.warning("No AgentCard provided, headers won't include auth")
            return

        # No security schemes means no authentication required.
        # (security_schemes is a proto map — use truthiness, not HasField.)
        if not agent_card.security_schemes:
            logger.info(f"Agent {agent_card.name} has no security schemes, sending request without authentication.")
            return

        # Detect authentication scheme
        try:
            auth_type, scheme_name, scheme_obj = self._detect_auth_scheme(agent_card)
        except ValueError as e:
            logger.warning(
                f"{e} "
                f"Available schemes: {list(agent_card.security_schemes.keys())}. "
                "Proceeding without auth header."
            )
            return

        # Handle OIDC authentication (pass user token directly)
        if auth_type == "oidc":
            await self._handle_oidc_auth(args, agent_card, scheme_name, scheme_obj)

    async def after(self, args: AfterArgs) -> None:
        """No-op: this interceptor only injects request headers in ``before``."""
        return

    @staticmethod
    def _set_header(args: BeforeArgs, name: str, value: str) -> None:
        """Inject a request header via the call context's service parameters."""
        if args.context is None:
            args.context = ClientCallContext()
        if args.context.service_parameters is None:
            args.context.service_parameters = {}
        args.context.service_parameters[name] = value

    async def _handle_oidc_auth(
        self,
        args: BeforeArgs,
        agent_card: AgentCard,
        scheme_name: str,
        scheme_obj: Any,
    ) -> None:
        """
        Handle OIDC authentication by exchanging user token for target-specific token.

        Token exchange targets:
        - voice-agent: Uses its own client ID as the target
        - All other agents: Uses 'orchestrator' target with reduced scopes

        This provides scope-based isolation while maintaining dynamic provisioning.

        Injects the resulting bearer token into ``args.context.service_parameters``.

        Args:
            args: The interceptor BeforeArgs (mutated to carry auth headers)
            agent_card: Target agent card
            scheme_name: Security scheme name (agent's client ID)
            scheme_obj: OpenID Connect scheme object
        """
        # Verify issuer matches configuration
        if not scheme_obj.open_id_connect_url.startswith(self.oidc_issuer):
            logger.warning(
                f"Agent {agent_card.name} uses different OIDC issuer: {scheme_obj.open_id_connect_url} "
                f"(expected: {self.oidc_issuer}). Proceeding without auth header."
            )
            return

        # Determine target client ID based on agent requirements
        # voice-agent uses its own client ID; others use a reduced-scope orchestrator token
        if scheme_name == "voice-agent":
            # NOTE: the voice-agent client is provisioned manually with its own audience/scopes
            target_client_id = scheme_name
            requested_scopes = ["openid", "profile", "email"]
            token_description = f"{scheme_name} token"
        elif scheme_name == "alloy-agent":
            # TOOD: shall we establish a convention that agents needing MCP gateway access should use a specific scheme name?
            # For now, we assume only the alloy-agent needs it and hardcode the logic here
            target_client_id = "gatana"  # This client is provisioned with MCP gateway access
            requested_scopes = ["openid", "profile", "offline_access"]  # MCP gateway scopes
            token_description = "gatana token (MCP gateway access)"
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

            # Inject token as a request header via the call context
            self._set_header(args, "Authorization", f"Bearer {exchanged_token}")

            # Add sub_agent_id as HTTP header for cost tracking
            if self.sub_agent_id:
                self._set_header(args, "X-Sub-Agent-Id", str(self.sub_agent_id))
                logger.debug(f"Added X-Sub-Agent-Id header: {self.sub_agent_id}")

            # Note: User context is already in token claims (sub, email, name, groups)
            # No need for additional headers

            logger.info(f"Successfully passing {token_description} to {agent_card.name}")

        except Exception as e:
            logger.error(
                f"Token exchange failed for {agent_card.name} (target={target_client_id}): {e}. "
                "Request will be sent without authentication and will likely fail.",
                exc_info=True,
            )

    def clear_cache(self):
        """Clear the token cache in the OAuth2 client."""
        self.oauth2_client.clear_cache()
