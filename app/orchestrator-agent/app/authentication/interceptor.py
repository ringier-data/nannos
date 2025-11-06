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

from typing import Any, Optional, TYPE_CHECKING
import logging

from a2a.client.middleware import ClientCallInterceptor, ClientCallContext
from a2a.types import AgentCard

from .agent_security_config import requires_token_exchange as check_requires_token_exchange

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
    
    def __init__(
        self,
        user_token: str,
        token_exchanger: Optional['OktaTokenExchanger'] = None,  # type: ignore
    ):
        """
        Initialize smart token interceptor.
        
        Args:
            user_token: User's authenticated JWT token
            token_exchanger: Optional OktaTokenExchanger for token exchange
                           Required if calling agents with OAuth2 security
        """
        self.user_token = user_token
        self.token_exchanger = token_exchanger
        
        # Cache of agent_name -> exchanged_token to avoid repeated exchanges
        self._token_cache: dict[str, str] = {}
    
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
        if 'headers' not in http_kwargs:
            http_kwargs['headers'] = {}
        
        # No agent card means we can't determine auth requirements
        if not agent_card:
            logger.warning("No AgentCard provided, headers won't include auth")
            return request_payload, http_kwargs
        
        agent_name = agent_card.name
        
        # Check if this agent requires token exchange
        needs_exchange = check_requires_token_exchange(agent_card)
        
        if not needs_exchange:
            # Agent doesn't require OAuth2 - no auth header needed
            logger.debug(
                f"Agent {agent_name} doesn't require OAuth2, "
                "skipping authentication"
            )
            return request_payload, http_kwargs
        
        # Token exchange required
        if not self.token_exchanger:
            logger.error(
                f"Agent {agent_name} requires token exchange but no "
                "token_exchanger provided. Request will fail."
            )
            return request_payload, http_kwargs
        
        # Check cache first
        if agent_name in self._token_cache:
            logger.debug(f"Using cached token for {agent_name}")
            http_kwargs['headers']['Authorization'] = f'Bearer {self._token_cache[agent_name]}'
            return request_payload, http_kwargs
        
        # Perform token exchange
        try:
            from .agent_security_config import get_agent_client_id, get_required_scopes
            
            target_client_id = get_agent_client_id(agent_card)
            required_scopes = get_required_scopes(agent_card)
            
            if not target_client_id:
                logger.error(
                    f"Cannot determine client_id for {agent_name}, "
                    "cannot exchange token. Request will fail."
                )
                return request_payload, http_kwargs
            
            logger.info(
                f"Exchanging token for {agent_name} "
                f"(target: {target_client_id}, scopes: {required_scopes})"
            )
            
            exchanged_token = await self.token_exchanger.exchange_token(
                subject_token=self.user_token,
                target_client_id=target_client_id,
                requested_scopes=required_scopes if required_scopes else None,
            )
            
            # Cache the exchanged token
            self._token_cache[agent_name] = exchanged_token
            
            # Add to headers
            http_kwargs['headers']['Authorization'] = f'Bearer {exchanged_token}'
            
            logger.info(f"Successfully exchanged token for {agent_name}")
            
        except Exception as e:
            logger.error(
                f"Token exchange failed for {agent_name}: {e}. "
                "Request will be sent without authentication and will likely fail."
            )
        
        return request_payload, http_kwargs
    
    def clear_cache(self):
        """Clear the token cache to force new exchanges."""
        self._token_cache.clear()
