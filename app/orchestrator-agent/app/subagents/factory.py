import asyncio
import logging
from typing import Any, AsyncIterable, Dict, Optional

from a2a.types import AgentCard
from langchain_core.runnables import RunnableLambda

from ..authentication import SmartTokenInterceptor
from .config import A2AClientConfig
from .runnable import A2AClientRunnable

logger = logging.getLogger(__name__)


def make_a2a_async_runnable(
    agent_card: AgentCard,
    *,
    user_token: Optional[str] = None,
    token_exchanger: Optional[Any] = None,
    config: Optional[A2AClientConfig] = None,
) -> RunnableLambda:
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
        token_exchanger: Optional OktaTokenExchanger for token exchange
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
        from ..authentication import AgentSecurityConfig

        security_config = AgentSecurityConfig(agent_card)
        security_summary = security_config.get_summary()

        logger.info(
            f"Auto-detected security config for {agent_card.name}: "
            f"OAuth2={security_summary['requires_oauth2']}, "
            f"TokenExchange={security_summary['requires_token_exchange']}"
        )

        config.auth_interceptor = SmartTokenInterceptor(
            user_token=user_token,
            token_exchanger=token_exchanger,
        )
        logger.debug(f"Using SmartTokenInterceptor for {agent_card.name}")

    else:
        logger.info(f"No user_token provided for {agent_card.name}. Agent will be called without authentication.")

    # Create the runnable (already has both ainvoke and astream)
    runnable = A2AClientRunnable(agent_card, config)

    # Create LangChain-compatible wrapper
    async def _async_wrapper(input_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.debug(f"_async_wrapper called with input: {input_data}")
        result = await runnable.ainvoke(input_data)
        logger.debug(f"_async_wrapper returning result: {result}")
        return result

    async def _async_stream_wrapper(input_data: Dict[str, Any]) -> AsyncIterable[Dict[str, Any]]:
        """Stream wrapper that can be used by LangChain streaming."""
        logger.debug(f"_async_stream_wrapper called with input: {input_data}")
        async for item in runnable.astream(input_data):
            logger.debug(f"_async_stream_wrapper yielding: {item.get('type')}")
            yield item

    def _sync_wrapper(input_data: Any) -> Dict[str, Any]:
        logger.debug(f"_sync_wrapper called with input: {input_data}")
        logger.debug(f"_sync_wrapper input type: {type(input_data)}")

        # Convert input to dict if needed
        if not isinstance(input_data, dict):
            logger.debug("Converting non-dict input to dict")
            input_data = {"input": str(input_data)}
        else:
            logger.debug(f"Input is already a dict with keys: {list(input_data.keys())}")

        # Try to extract context_id from LangChain config if available
        # Note: DeepAgents' task tool creates isolated contexts, so this may not be available
        if "config" in input_data and isinstance(input_data["config"], dict):
            langchain_config = input_data["config"]
            if "configurable" in langchain_config and "thread_id" in langchain_config["configurable"]:
                context_id = langchain_config["configurable"]["thread_id"]
                input_data["context_id"] = context_id
                logger.debug(f"Extracted context_id from LangChain config: {context_id}")

        logger.debug(f"Calling asyncio.run with input: {input_data}")
        result = asyncio.run(_async_wrapper(input_data))
        logger.debug(f"_sync_wrapper returning result: {result}")
        return result

    # Create a RunnableLambda with streaming support
    # Note: The _async_stream_wrapper is available but not directly exposed through RunnableLambda
    # DeepAgents middleware will need to detect and use the runnable.astream() method
    wrapped_runnable = RunnableLambda(_sync_wrapper)

    # Attach the streaming runnable for middleware access
    wrapped_runnable._streaming_runnable = runnable  # type: ignore

    return wrapped_runnable
