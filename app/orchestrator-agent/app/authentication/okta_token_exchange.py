"""
OAuth2 Token Exchange (RFC 8693) for Okta OIDC.

This module implements token exchange to obtain service-specific tokens
for sub-agent communication. Each component has its own OIDC client in Okta,
and only accepts tokens minted for that specific client.

Architecture:
1. User authenticates with Okta → receives user JWT (for orchestrator)
2. Orchestrator validates user JWT using its client_secret
3. To call JIRA sub-agent:
   - Orchestrator exchanges user JWT for JIRA-specific token
   - Uses RFC 8693 token exchange with JIRA's audience
   - Receives new JWT minted for JIRA's client_id
4. JIRA sub-agent validates token using its own client_secret

This provides:
- Service isolation (each service validates only tokens for itself)
- Token scoping (tokens have limited audience)
- Zero-trust (no service trusts another's tokens)
- Audit trail (each token exchange is logged)

Reference: https://developer.okta.com/docs/guides/set-up-token-exchange/-/main/
"""

import logging
from typing import Optional, Dict, List
from datetime import datetime, timedelta
import asyncio

import httpx
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


class TokenExchangeError(Exception):
    """Raised when token exchange fails."""
    pass


class ExchangedToken(BaseModel):
    """Result of a successful token exchange."""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str
    issued_token_type: str = "urn:ietf:params:oauth:token-type:access_token"
    
    # Computed fields
    expires_at: datetime = Field(default_factory=datetime.utcnow)
    
    def __init__(self, **data):
        super().__init__(**data)
        # Calculate expiration time
        self.expires_at = datetime.utcnow() + timedelta(seconds=self.expires_in)
    
    def is_expired(self, buffer_seconds: int = 60) -> bool:
        """Check if token is expired (with buffer for clock skew)."""
        return datetime.utcnow() >= (self.expires_at - timedelta(seconds=buffer_seconds))


class OktaTokenExchanger:
    """
    Handles OAuth2 Token Exchange (RFC 8693) with Okta.
    
    This class manages the token exchange flow for obtaining service-specific
    tokens from a user's authenticated token. It maintains a cache of exchanged
    tokens to avoid unnecessary exchanges.
    
    Configuration via environment variables:
    - OKTA_DOMAIN: Okta domain (e.g., rcplus.okta.com)
    - OKTA_CLIENT_ID: OAuth2 client ID for this service (orchestrator)
    - OKTA_CLIENT_SECRET: Client secret for this service (orchestrator)
    - OKTA_ISSUER: Optional custom issuer (default: https://{domain}/oauth2/default)
    
    Usage:
        exchanger = OktaTokenExchanger(
            client_id="orchestrator_client_id",
            client_secret="orchestrator_secret",
            okta_domain="rcplus.okta.com"
        )
        
        # Exchange user token for JIRA-specific token
        jira_token = await exchanger.exchange_token(
            subject_token=user_jwt,
            target_client_id="jira_client_id",
            requested_scopes=["jira:read", "jira:write"]
        )
    """
    
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        okta_domain: str,
        issuer: Optional[str] = None,
        httpx_client: Optional[httpx.AsyncClient] = None,
        cache_tokens: bool = True,
    ):
        """
        Initialize the token exchanger.
        
        Args:
            client_id: This service's OAuth2 client ID
            client_secret: This service's client secret
            okta_domain: Okta domain (e.g., rcplus.okta.com)
            issuer: Optional custom issuer (default: https://{domain}/oauth2/default)
            httpx_client: Optional HTTP client (will create if not provided)
            cache_tokens: Whether to cache exchanged tokens (default: True)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.okta_domain = okta_domain
        self.issuer = issuer or f'https://{okta_domain}/oauth2/default'
        self.token_endpoint = f'{self.issuer}/v1/token'
        
        self._httpx_client = httpx_client
        self._close_httpx_client = httpx_client is None
        self._cache_tokens = cache_tokens
        
        # Token cache: {cache_key: ExchangedToken}
        self._token_cache: Dict[str, ExchangedToken] = {}
        self._cache_lock = asyncio.Lock()
        
        logger.info("OktaTokenExchanger initialized")
        logger.info(f"  Issuer: {self.issuer}")
        logger.info(f"  Client ID: {self.client_id}")
        logger.info(f"  Token caching: {'enabled' if cache_tokens else 'disabled'}")
    
    async def _get_httpx_client(self) -> httpx.AsyncClient:
        """Lazy initialization of HTTP client."""
        if self._httpx_client is None:
            self._httpx_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=30.0)
            )
        return self._httpx_client
    
    def _get_cache_key(
        self,
        subject_token: str,
        target_client_id: str,
        requested_scopes: Optional[List[str]] = None,
    ) -> str:
        """Generate cache key for token lookup."""
        # Use first 16 chars of subject token + target client + scopes as key
        token_prefix = subject_token[:16] if len(subject_token) > 16 else subject_token
        scopes_str = ",".join(sorted(requested_scopes or []))
        return f"{token_prefix}:{target_client_id}:{scopes_str}"
    
    async def exchange_token(
        self,
        subject_token: str,
        target_client_id: str,
        requested_scopes: Optional[List[str]] = None,
        actor_token: Optional[str] = None,
        force_refresh: bool = False,
    ) -> str:
        """
        Exchange a subject token for a target service-specific token.
        
        Implements OAuth2 Token Exchange (RFC 8693) to obtain a new access token
        that is valid for the target service. The exchanged token will have:
        - audience (aud) claim set to target_client_id
        - scopes limited to requested_scopes
        - same user context (sub claim) as original token
        
        Args:
            subject_token: The user's authenticated JWT token
            target_client_id: The target service's OAuth2 client ID
            requested_scopes: Optional list of scopes to request (e.g., ["jira:read"])
            actor_token: Optional actor token for delegation scenarios
            force_refresh: Force token exchange even if cached token exists
            
        Returns:
            The exchanged access token string (JWT) for the target service
            
        Raises:
            TokenExchangeError: If token exchange fails
        """
        # Check cache first (unless force_refresh)
        if self._cache_tokens and not force_refresh:
            cache_key = self._get_cache_key(subject_token, target_client_id, requested_scopes)
            async with self._cache_lock:
                cached_token = self._token_cache.get(cache_key)
                if cached_token and not cached_token.is_expired():
                    logger.debug(f"Using cached token for {target_client_id}")
                    return cached_token.access_token
                elif cached_token:
                    logger.debug(f"Cached token for {target_client_id} expired, refreshing")
                    del self._token_cache[cache_key]
        
        # Perform token exchange
        logger.info(f"Exchanging token for target client: {target_client_id}")
        
        client = await self._get_httpx_client()
        
        # Prepare token exchange request (RFC 8693)
        data = {
            'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
            'subject_token': subject_token,
            'subject_token_type': 'urn:ietf:params:oauth:token-type:access_token',
            'audience': target_client_id,  # Critical: limits token to target service
        }
        
        # Add optional parameters
        if requested_scopes:
            data['scope'] = ' '.join(requested_scopes)
        
        if actor_token:
            data['actor_token'] = actor_token
            data['actor_token_type'] = 'urn:ietf:params:oauth:token-type:access_token'
        
        # Basic auth with our client credentials
        auth = (self.client_id, self.client_secret)
        
        try:
            response = await client.post(
                self.token_endpoint,
                data=data,
                auth=auth,
                headers={'Accept': 'application/json'},
            )
            
            if response.status_code != 200:
                error_data = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
                error_msg = error_data.get('error_description', error_data.get('error', response.text))
                logger.error(f"Token exchange failed: {error_msg}")
                raise TokenExchangeError(
                    f"Token exchange failed for {target_client_id}: {error_msg}"
                )
            
            token_data = response.json()
            exchanged_token = ExchangedToken(**token_data)
            
            # Cache the token
            if self._cache_tokens:
                cache_key = self._get_cache_key(subject_token, target_client_id, requested_scopes)
                async with self._cache_lock:
                    self._token_cache[cache_key] = exchanged_token
                    logger.debug(f"Cached exchanged token for {target_client_id}, expires in {exchanged_token.expires_in}s")
            
            logger.info(f"Successfully exchanged token for {target_client_id}")
            return exchanged_token.access_token
            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error during token exchange: {e}")
            raise TokenExchangeError(f"Network error during token exchange: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error during token exchange: {e}")
            raise TokenExchangeError(f"Token exchange failed: {e}") from e
    
    async def clear_cache(self):
        """Clear all cached tokens."""
        async with self._cache_lock:
            self._token_cache.clear()
            logger.debug("Cleared token cache")
    
    async def close(self):
        """Clean up resources."""
        if self._close_httpx_client and self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None


class SubAgentTokenProvider:
    """
    Token provider for sub-agent communication with automatic token exchange.
    
    This class is designed to provide tokens on-demand for each sub-agent.
    It maintains a mapping of sub-agent names to their OAuth2 client IDs
    and performs token exchange automatically.
    
    Note: This class is largely superseded by SmartTokenInterceptor which
    automatically detects auth requirements from AgentCard. This class is
    kept for backward compatibility with manual configurations.
    
    Usage:
        provider = SubAgentTokenProvider(
            token_exchanger=exchanger,
            user_token=user_jwt,
            subagent_clients={
                "jira": "jira_client_id",
                "confluence": "confluence_client_id"
            }
        )
        
        # Get token manually
        token = await provider.get_token_for_agent(agent_card)
    """
    
    def __init__(
        self,
        token_exchanger: OktaTokenExchanger,
        user_token: str,
        subagent_clients: Dict[str, str],
        default_scopes: Optional[Dict[str, List[str]]] = None,
    ):
        """
        Initialize the sub-agent token provider.
        
        Args:
            token_exchanger: OktaTokenExchanger instance
            user_token: User's authenticated JWT token
            subagent_clients: Mapping of sub-agent name -> client_id
            default_scopes: Optional mapping of sub-agent name -> requested scopes
        """
        self.token_exchanger = token_exchanger
        self.user_token = user_token
        self.subagent_clients = subagent_clients
        self.default_scopes = default_scopes or {}
    
    async def get_token_for_agent(self, agent_card) -> str:
        """
        Get an exchanged token for a specific sub-agent.
        
        Args:
            agent_card: AgentCard for the target sub-agent
            
        Returns:
            Exchanged token string for the sub-agent
            
        Raises:
            TokenExchangeError: If token exchange fails or agent not configured
        """
        if not agent_card:
            raise TokenExchangeError("AgentCard is required for token exchange")
        
        agent_name = agent_card.name
        
        # Look up client ID for this sub-agent
        target_client_id = self.subagent_clients.get(agent_name)
        if not target_client_id:
            raise TokenExchangeError(
                f"No OAuth2 client configured for sub-agent: {agent_name}. "
                f"Available agents: {list(self.subagent_clients.keys())}"
            )
        
        # Get scopes for this sub-agent
        scopes = self.default_scopes.get(agent_name)
        
        # Exchange token
        logger.debug(f"Getting token for sub-agent: {agent_name} (client: {target_client_id})")
        return await self.token_exchanger.exchange_token(
            subject_token=self.user_token,
            target_client_id=target_client_id,
            requested_scopes=scopes,
        )
