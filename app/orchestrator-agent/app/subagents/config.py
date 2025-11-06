"""
Configuration for A2A client.

Provides configuration options for timeouts, authentication, telemetry,
and circuit breaker functionality for Agent-to-Agent communication.
"""
from typing import Any


class A2AClientConfig:
    """Configuration for A2A client.
    
    Note: Retry logic should be configured at the LangGraph level using retry policies,
    not within this A2A client wrapper. See LangGraph documentation:
    https://langchain-ai.github.io/langgraph/how-tos/graph-api/#add-retry-policies
    
    Example:
        from langgraph.types import RetryPolicy
        
        graph = create_deep_agent(...).with_retry(
            retry_policy=RetryPolicy(
                max_attempts=3,
                backoff_factor=2.0,
                retry_on=Exception
            )
        )
    """
    
    def __init__(
        self,
        *,
        timeout_connect: float = 10.0,
        timeout_read: float = 60.0,
        timeout_write: float = 10.0,
        timeout_pool: float = 5.0,
        auth_interceptor: Any = None,
        user_agent_prefix: str = "Orchestrator/1.0",
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: float = 30.0,
    ):
        """Initialize A2A configuration.
        
        Args:
            timeout_connect: Connection timeout in seconds (default: 10.0)
            timeout_read: Read timeout in seconds (default: 60.0)
            timeout_write: Write timeout in seconds (default: 10.0)
            timeout_pool: Connection pool timeout in seconds (default: 5.0)
            auth_interceptor: Optional authentication interceptor
            user_agent_prefix: User agent prefix for HTTP requests (default: "Orchestrator/1.0")
            circuit_breaker_threshold: Number of failures before circuit opens (default: 5)
            circuit_breaker_timeout: Seconds before circuit attempts to close (default: 30.0)
        """
        self.timeout_connect = timeout_connect
        self.timeout_read = timeout_read
        self.timeout_write = timeout_write
        self.timeout_pool = timeout_pool
        self.auth_interceptor = auth_interceptor
        self.user_agent_prefix = user_agent_prefix
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_timeout = circuit_breaker_timeout
