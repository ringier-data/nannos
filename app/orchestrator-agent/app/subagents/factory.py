import logging
from typing import Any, Optional

from a2a.types import AgentCard
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..authentication import SmartTokenInterceptor
from .config import A2AClientConfig
from .runnable import A2AClientRunnable

logger = logging.getLogger(__name__)


def make_a2a_async_runnable(
    agent_card: AgentCard,
    oauth2_client: OidcOAuth2Client,
    *,
    user_token: Optional[str] = None,
    user_context: Optional[dict[str, Any]] = None,
    config: Optional[A2AClientConfig] = None,
) -> A2AClientRunnable:
    """
    Creates an A2A Runnable with automatic authentication.

    This factory automatically detects authentication requirements from the target
    agent's AgentCard security configuration and configures the appropriate auth
    strategy. It supports:

    - **OAuth2 with Token Exchange**: When agent requires specific client_id tokens
    - **No Authentication**: When agent has no security requirements

    The authentication strategy is determined by examining AgentCard.security_schemes.
    If OAuth2 security is configured and a token_exchanger is provided, it will
    automatically perform RFC 8693 token exchange to obtain service-specific tokens.

    Features:
    - Automatic auth detection from AgentCard
    - OAuth2 token exchange (RFC 8693) support
    - Token caching per agent
    - Proper A2A SDK usage (Message, Task, TaskState enums)
    - Streaming response handling
    - Resource cleanup and connection pooling

    Args:
        agent_card: The AgentCard describing the target agent
        user_token: User's authenticated access token (required for OAuth2 agents)
        user_context: Optional user context dict with user_id, email, name
        config: Optional A2AClientConfig for advanced configuration

    Returns:
        A RunnableLambda that can be used with LangChain, supporting async execution
        with intelligent authentication handling.

    Examples:
        # With token exchange (recommended for OAuth2 agents)
        runnable = make_a2a_async_runnable(
            agent_card=jira_card,
            user_token=user_access_token,
            token_exchanger=exchanger,
        )

        # No authentication (public endpoint)
        runnable = make_a2a_async_runnable(agent_card=public_agent_card)
    """
    logger.debug(f"Creating A2A runnable for agent: {agent_card.name} ({agent_card.url})")

    # Set up configuration
    if config is None:
        config = A2AClientConfig()

    # Auto-detect authentication strategy
    if user_token:
        config.auth_interceptor = SmartTokenInterceptor(
            user_token=user_token,
            user_context=user_context,
            oauth2_client=oauth2_client,
        )
        logger.debug(f"Using SmartTokenInterceptor for {agent_card.name}")

    else:
        logger.info(f"No user_token provided for {agent_card.name}. Agent will be called without authentication.")

    # Create and return the async runnable directly
    # A2AClientRunnable already implements both ainvoke and astream
    runnable = A2AClientRunnable(agent_card, config)

    return runnable
